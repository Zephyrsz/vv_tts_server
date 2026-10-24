"""Read-only macOS experiment. No HTTP, playback, or iPhone measurement."""
import argparse
import base64
import gc
import hashlib
import io
import json
import pathlib
import platform
import resource
import statistics
import subprocess
import sys
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[3]
TEXTS = {
    'short': 'こんにちは。',
    'medium': '今日はいい天気ですね。図書館で日本語の本を読んでいます。',
    'long': '朝、窓を開けると、涼しい風が部屋に入ってきました。今日は図書館に行って、日本の歴史について調べる予定です。駅までの道には、小さなパン屋さんや花屋さんが並んでいます。電車に乗っている間も、本を読みながら新しい言葉を覚えています。午後は友達と公園を散歩して、夕方には家に帰ります。',
}


def rss_mib():
    return int(subprocess.check_output(['ps', '-o', 'rss=', '-p', str(__import__('os').getpid())])) / 1024


def wav_info(data):
    with wave.open(io.BytesIO(data)) as w:
        return {'duration_s': w.getnframes()/w.getframerate(), 'rate': w.getframerate(),
                'channels': w.getnchannels(), 'sample_width': w.getsampwidth(), 'bytes': len(data)}


def worker(threads, assets_dir=None):
    sys.path.insert(0, str(ROOT))
    t = time.perf_counter()
    from local_engine import LocalVoicevoxEngine, split_wav
    import_ms = (time.perf_counter()-t)*1000
    t = time.perf_counter()
    engine = LocalVoicevoxEngine(pathlib.Path(assets_dir) if assets_dir else ROOT/'assets', ROOT/'assets/models', threads)
    init_ms = (time.perf_counter()-t)*1000
    result = {'threads': threads, 'import_ms': import_ms, 'engine_init_ms': init_ms,
              'rss_after_init_mib': rss_mib(), 'supported_devices': repr(engine.synthesizer.onnxruntime.supported_devices()),
              'is_gpu_mode': engine.synthesizer.is_gpu_mode, 'first_calls': [], 'warm_cases': []}
    # File caches may already be warm. These are first calls in a new process, not cold disk timings.
    for style in [0, 8, 14, 24]:
        t = time.perf_counter()
        audio = engine.synthesize(TEXTS['medium'], style, {})
        result['first_calls'].append({'style': style, 'elapsed_ms': (time.perf_counter()-t)*1000,
                                     'rss_mib': rss_mib(), **wav_info(audio)})
    for label, text in TEXTS.items():
        for style in ([0, 8, 14, 24] if label == 'medium' else [0]):
            phases = []
            for _ in range(3):
                t0 = time.perf_counter()
                query = engine.synthesizer.create_audio_query(text, style)
                query.validate()
                t1 = time.perf_counter()
                audio = engine.synthesizer.synthesis(query, style)
                t2 = time.perf_counter()
                phases.append({'query_ms': (t1-t0)*1000, 'synthesis_ms': (t2-t1)*1000,
                               'total_ms': (t2-t0)*1000})
            info = wav_info(audio)
            med = {k: statistics.median(x[k] for x in phases) for k in phases[0]}
            result['warm_cases'].append({'label': label, 'style': style, 'characters': len(text),
                                         **info, 'median': med, 'rtf': med['total_ms']/1000/info['duration_s'],
                                         'runs': phases})
    # Approximate allocation/encoding costs separately; excludes sockets and browser audio.
    audio = engine.synthesize(TEXTS['medium'], 0, {})
    overhead = {}
    for name, fn in {
        'base64_ms': lambda: base64.b64encode(audio).decode('utf-8'),
        'json_with_base64_ms': lambda: json.dumps({'data': base64.b64encode(audio).decode('utf-8')}),
        'split_300ms_ms': lambda: list(split_wav(audio, 0.3)),
    }.items():
        times = []
        for _ in range(30):
            t = time.perf_counter(); fn(); times.append((time.perf_counter()-t)*1000)
        overhead[name] = statistics.median(times)
    result['encoding'] = {**overhead, **wav_info(audio)}
    gc.collect()
    result['rss_end_mib'] = rss_mib()
    # macOS ru_maxrss is bytes. Linux uses KiB; this experiment is macOS only.
    result['peak_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024*1024)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker-threads', type=int)
    parser.add_argument('--assets-dir')
    args = parser.parse_args()
    if args.worker_threads:
        print(json.dumps(worker(args.worker_threads, args.assets_dir), ensure_ascii=False))
    else:
        results = []
        for threads in [1, 2, 4]:
            p = subprocess.run([sys.executable, __file__, '--worker-threads', str(threads)],
                               capture_output=True, text=True, check=True)
            results.append(json.loads(p.stdout))
            print('Finished threads', threads, file=sys.stderr, flush=True)
        manifest = []
        for path in sorted((ROOT/'assets/models').glob('*.vvm')):
            manifest.append({'file': path.name, 'bytes': path.stat().st_size,
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        final = {'date_local': '2026-10-24 Asia/Shanghai', 'platform': platform.platform(),
                 'python': sys.version, 'cpu': subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip(),
                 'ram_bytes': int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'])),
                 'project_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
                 'texts': TEXTS, 'models': manifest, 'results': results,
                 'limitations': ['3 warm repetitions only; no p95 or speedup claim', 'process first call, warm filesystem cache',
                                 'macOS CPU only; no iPhone, ANE, energy, or playback measurement',
                                 'RSS includes Python, three loaded model sessions, dictionary and runtime; not iOS budget',
                                 'query includes Open JTalk plus duration/intonation; synthesis includes more than decoder alone']}
        output = pathlib.Path(__file__).with_name('macos_baseline.json')
        output.write_text(json.dumps(final, ensure_ascii=False, indent=2)+'\n')
        print(output)
