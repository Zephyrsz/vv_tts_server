"""Local VOICEVOX WebSocket TTS server."""
import asyncio
import json
import logging
import ssl
import subprocess
import time
import yaml
from collections import defaultdict
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Optional, Set

import websockets

from local_engine import LocalVoicevoxEngine, split_wav

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)

logger = logging.getLogger(__name__)

# 加载配置
config_path = Path(__file__).parent / "config.yaml"
if not config_path.exists():
    logger.error(f"配置文件不存在: {config_path}")
    raise FileNotFoundError(f"Missing config file: {config_path}")

with open(config_path, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# 配置参数
HOST = config['server']['host']
PORT = config['server']['port']
CERT_FILE = config['server']['ssl']['cert_file']
KEY_FILE = config['server']['ssl']['key_file']

DEFAULT_SEGMENT_LENGTH = config['streaming']['default_segment_length']
MIN_SEGMENT_LENGTH = config['streaming']['min_segment_length']
MAX_SEGMENT_LENGTH = config['streaming']['max_segment_length']

MAX_TEXT_LENGTH = config['limits']['max_text_length']
MAX_CONNECTIONS = config['limits']['max_connections']

RATE_LIMIT_ENABLED = config['rate_limit']['enabled']
RATE_LIMIT_RATE = config['rate_limit']['rate']
RATE_LIMIT_BURST = config['rate_limit']['burst']

# TTS Engine 配置
ASSETS_DIR = Path(__file__).parent / config['tts_engine']['assets_dir']
MODEL_DIR = Path(__file__).parent / config['tts_engine']['model_dir']
CPU_NUM_THREADS = config.get('tts_engine', {}).get('cpu_num_threads', 0)


def ensure_tls_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    if not cert_path.exists() and not key_path.exists():
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-nodes", "-days", "365", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout", str(key_path), "-out", str(cert_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("Could not generate the local TLS certificate") from exc
        key_path.chmod(0o600)
    elif not cert_path.is_file() or not key_path.is_file():
        raise FileNotFoundError("Both TLS certificate and key are required")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def serve_page(connection, request):
    if request.path == "/ws":
        return None
    if request.path == "/":
        html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
        response = connection.respond(HTTPStatus.OK, html)
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Cache-Control"] = "no-store"
        return response
    return connection.respond(HTTPStatus.NOT_FOUND, "Not Found")


class RateLimiter:
    """令牌桶速率限制器"""

    def __init__(self, rate: float = 10, burst: int = 20):
        self.rate = rate
        self.burst = burst
        self.buckets = defaultdict(lambda: {'tokens': burst, 'last': time.time()})

    def allow(self, client_id: str) -> bool:
        """检查是否允许请求"""
        bucket = self.buckets[client_id]
        now = time.time()
        elapsed = now - bucket['last']

        bucket['tokens'] = min(self.burst, bucket['tokens'] + elapsed * self.rate)
        bucket['last'] = now

        if bucket['tokens'] >= 1:
            bucket['tokens'] -= 1
            return True
        return False


class VoicevoxStreamingServer:
    """VOICEVOX 流式 WebSocket 服务器"""

    def __init__(self, tts_engine: LocalVoicevoxEngine):
        self.tts_engine = tts_engine
        self.active_connections: Set[websockets.WebSocketServerProtocol] = set()
        self.rate_limiter = RateLimiter(rate=RATE_LIMIT_RATE, burst=RATE_LIMIT_BURST) if RATE_LIMIT_ENABLED else None

    async def handle_client(self, websocket):
        """处理客户端连接"""
        client_id = id(websocket)
        client_addr = websocket.remote_address

        if len(self.active_connections) >= MAX_CONNECTIONS:
            await websocket.close(1008, "Server at capacity")
            logger.warning(f"拒绝连接 {client_addr}: 达到最大连接数")
            return

        self.active_connections.add(websocket)
        logger.info(f"客户端 {client_addr} (ID: {client_id}) 已连接,当前连接数: {len(self.active_connections)}")

        try:
            async for message in websocket:
                await self.process_message(websocket, client_id, message)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"客户端 {client_addr} 断开连接: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"处理客户端 {client_addr} 时出错: {e}", exc_info=True)
        finally:
            self.active_connections.discard(websocket)
            logger.info(f"客户端 {client_addr} 已移除,当前连接数: {len(self.active_connections)}")

    async def process_message(self, websocket, client_id: int, message: str):
        """处理客户端消息"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')

            # 速率限制检查
            if self.rate_limiter and msg_type in ('synthesize', 'stream_synthesize'):
                if not self.rate_limiter.allow(str(client_id)):
                    await self.send_error(websocket, "Rate limit exceeded", request_id=data.get('request_id'))
                    return

            if msg_type == 'synthesize':
                await self.handle_synthesis(websocket, data)
            elif msg_type == 'stream_synthesize':
                await self.handle_stream_synthesis(websocket, data)
            elif msg_type == 'get_speakers':
                await self.handle_get_speakers(websocket)
            elif msg_type == 'ping':
                await self.handle_ping(websocket)
            else:
                await self.send_error(websocket, f"Unknown message type: {msg_type}", request_id=data.get('request_id'))

        except json.JSONDecodeError:
            await self.send_error(websocket, "Invalid JSON format")
        except Exception as e:
            logger.error(f"处理消息时出错: {e}", exc_info=True)
            await self.send_error(websocket, str(e))

    async def handle_synthesis(self, websocket, data: dict):
        """处理完整合成请求"""
        try:
            text = data.get('text', '')
            speaker = data.get('speaker', 0)
            params = data.get('params', {})
            request_id = data.get('request_id')

            # 验证输入
            if not text:
                await self.send_error(websocket, "Text cannot be empty", request_id=request_id)
                return

            if len(text) > MAX_TEXT_LENGTH:
                await self.send_error(websocket, f"Text too long (max {MAX_TEXT_LENGTH} characters)", request_id=request_id)
                return

            start_time = time.time()

            wav_data = await asyncio.to_thread(self.tts_engine.synthesize, text, speaker, params)

            # 4. Base64 编码
            import base64
            audio_b64 = base64.b64encode(wav_data).decode('utf-8')

            # 5. 发送响应
            elapsed_ms = int((time.time() - start_time) * 1000)
            response = {
                'type': 'audio',
                'request_id': request_id,
                'data': audio_b64,
                'format': 'wav',
                'sampleRate': 24000,
                'duration_ms': elapsed_ms
            }
            await websocket.send(json.dumps(response))
            logger.info(f"合成完成: {len(text)} 字符, {len(wav_data)} bytes, {elapsed_ms}ms, speaker {speaker}")

        except Exception as e:
            logger.error(f"合成失败: {e}", exc_info=True)
            await self.send_error(websocket, f"Synthesis failed: {str(e)}", request_id=data.get('request_id'))

    async def handle_stream_synthesis(self, websocket, data: dict):
        """处理流式合成请求"""
        request_id = data.get('request_id')

        try:
            text = data.get('text', '')
            speaker = data.get('speaker', 0)
            params = data.get('params', {})
            segment_length = params.get('segmentLength', DEFAULT_SEGMENT_LENGTH)

            # 验证输入
            if not text:
                await self.send_error(websocket, "Text cannot be empty", request_id=request_id)
                return

            if len(text) > MAX_TEXT_LENGTH:
                await self.send_error(websocket, f"Text too long (max {MAX_TEXT_LENGTH} characters)", request_id=request_id)
                return

            # 限制分段长度范围
            segment_length = max(MIN_SEGMENT_LENGTH, min(MAX_SEGMENT_LENGTH, segment_length))

            start_time = time.time()

            wav_data = await asyncio.to_thread(self.tts_engine.synthesize, text, speaker, params)

            # 3. 发送开始标记
            await websocket.send(json.dumps({
                'type': 'stream_start',
                'request_id': request_id,
                'sampleRate': 24000,
                'format': 'wav',
                'segmentLength': segment_length
            }))

            # 4. 流式合成并推送
            chunk_index = 0
            total_bytes = 0

            for chunk in split_wav(wav_data, segment_length):
                # 发送音频数据（二进制帧）
                await websocket.send(chunk)
                chunk_index += 1
                total_bytes += len(chunk)
                logger.debug(f"发送音频块 {chunk_index}: {len(chunk)} bytes")

            # 5. 发送结束标记
            elapsed_ms = int((time.time() - start_time) * 1000)
            await websocket.send(json.dumps({
                'type': 'stream_end',
                'request_id': request_id,
                'total_chunks': chunk_index,
                'total_bytes': total_bytes,
                'duration_ms': elapsed_ms
            }))

            logger.info(f"流式合成完成: {len(text)} 字符, {chunk_index} 块, {total_bytes} bytes, {elapsed_ms}ms, speaker {speaker}")

        except Exception as e:
            logger.error(f"流式合成失败: {e}", exc_info=True)
            await self.send_error(websocket, f"Streaming synthesis failed: {str(e)}", request_id=request_id)

    async def handle_get_speakers(self, websocket):
        """处理获取说话人列表请求"""
        try:
            speakers_data = self.tts_engine.get_speakers()

            await websocket.send(json.dumps({
                'type': 'speakers',
                'data': speakers_data
            }))
            logger.info("成功返回说话人列表")

        except Exception as e:
            logger.error(f"获取说话人失败: {e}")
            await self.send_error(websocket, f"Failed to get speakers: {str(e)}")

    async def handle_ping(self, websocket):
        """处理心跳请求"""
        await websocket.send(json.dumps({
            'type': 'pong',
            'timestamp': datetime.now().isoformat()
        }))

    async def send_error(self, websocket, message: str, request_id: Optional[str] = None):
        """发送错误消息"""
        error_msg = {
            'type': 'error',
            'message': message,
            'timestamp': datetime.now().isoformat()
        }
        if request_id:
            error_msg['request_id'] = request_id

        await websocket.send(json.dumps(error_msg))
        logger.warning(f"发送错误: {message}")


async def main():
    """启动 WebSocket 服务器"""
    logger.info("=" * 60)
    logger.info("VOICEVOX 本地 HTTPS/WSS TTS 服务启动")
    logger.info("=" * 60)

    # 初始化 TTS 引擎
    logger.info(f"正在加载 TTS 引擎...")
    logger.info(f"  模型目录: {MODEL_DIR}")
    logger.info(f"  运行资产: {ASSETS_DIR}")
    logger.info(f"  CPU 线程数: {CPU_NUM_THREADS if CPU_NUM_THREADS > 0 else 4}")

    try:
        tts_engine = LocalVoicevoxEngine(ASSETS_DIR, MODEL_DIR, CPU_NUM_THREADS)
        logger.info("✓ TTS 引擎加载成功")

        # 获取说话人信息
        speakers = tts_engine.get_speakers()
        logger.info(f"✓ 可用说话人数: {len(speakers)}")

    except Exception as e:
        logger.error(f"✗ TTS 引擎初始化失败: {e}", exc_info=True)
        raise RuntimeError("Local VOICEVOX assets could not be loaded") from e

    server = VoicevoxStreamingServer(tts_engine)

    ssl_context = ensure_tls_context(
        Path(__file__).parent / CERT_FILE,
        Path(__file__).parent / KEY_FILE,
    )

    logger.info("=" * 60)
    logger.info(f"测试页面: https://{HOST}:{PORT}/")
    logger.info(f"WebSocket: wss://{HOST}:{PORT}/ws")
    logger.info(f"最大连接数: {MAX_CONNECTIONS}")
    logger.info(f"速率限制: {'启用' if RATE_LIMIT_ENABLED else '禁用'}")
    if RATE_LIMIT_ENABLED:
        logger.info(f"  - 速率: {RATE_LIMIT_RATE} req/s")
        logger.info(f"  - 突发: {RATE_LIMIT_BURST} req")
    logger.info(f"流式配置:")
    logger.info(f"  - 默认分段长度: {DEFAULT_SEGMENT_LENGTH}s")
    logger.info(f"  - 分段范围: {MIN_SEGMENT_LENGTH}s - {MAX_SEGMENT_LENGTH}s")
    logger.info("=" * 60)

    async with websockets.serve(
        server.handle_client,
        HOST,
        PORT,
        ssl=ssl_context,
        process_request=serve_page,
        max_size=10 * 1024 * 1024,  # 10MB
        ping_interval=20,
        ping_timeout=10
    ):
        logger.info("✓ 服务器运行中,按 Ctrl+C 停止")
        await asyncio.Future()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='VOICEVOX Local HTTPS/WSS TTS Server')
    parser.add_argument('--debug', action='store_true', help='启用调试日志')
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n服务器已停止")
