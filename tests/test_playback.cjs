const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const page = resolve(__dirname, '../index.html');
const script = readFileSync(page, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

function wav(samples, rate = 24000) {
    // Include an odd-size unknown chunk: PCM is not always at byte 44.
    const bytes = new ArrayBuffer(54 + samples.length * 2);
    const view = new DataView(bytes);
    const tag = (offset, value) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
    tag(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); tag(8, 'WAVE');
    tag(12, 'JUNK'); view.setUint32(16, 1, true); view.setUint8(20, 123);
    tag(22, 'fmt '); view.setUint32(26, 16, true); view.setUint16(30, 1, true);
    view.setUint16(32, 1, true); view.setUint32(34, rate, true);
    view.setUint32(38, rate * 2, true); view.setUint16(42, 2, true); view.setUint16(44, 16, true);
    tag(46, 'data'); view.setUint32(50, samples.length * 2, true);
    samples.forEach((s, i) => view.setInt16(54 + i * 2, s, true));
    return bytes;
}

function fixture(contextRate = 24000) {
    const nodes = [];
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            value: id === 'text' ? 'こんにちは' : '1', disabled: false,
            textContent: '', className: '', children: [], paused: true,
            appendChild(child) { this.children.push(child); },
            replaceChildren() {}, removeAttribute() {}, load() {},
            pause() { this.paused = true; },
            play() { this.paused = false; return Promise.resolve(); },
        });
        return elements.get(id);
    };
    const ctx = {
        sampleRate: contextRate, currentTime: 10, state: 'running', destination: {},
        resume: async () => {},
        createBuffer(channels, length, sampleRate) {
            const data = Array.from({ length: channels }, () => new Float32Array(length));
            return { length, sampleRate, numberOfChannels: channels, duration: length / sampleRate,
                getChannelData: channel => data[channel],
                copyToChannel: (values, channel) => data[channel].set(values) };
        },
        createBufferSource() {
            const node = { stopped: false, disconnected: false,
                connect() {}, disconnect() { this.disconnected = true; },
                start(when) { this.when = when ?? ctx.currentTime; nodes.push(this); },
                stop() { this.stopped = true; } };
            return node;
        },
        // Old code takes this path; the fixed player must preserve the raw PCM.
        async decodeAudioData() {
            return this.createBuffer(1, 7200, contextRate);
        },
    };
    const sent = [];
    const sandbox = vm.createContext({
        console, ArrayBuffer, DataView, Float32Array, Uint8Array, Set,
        Blob, URL, atob, setTimeout, clearTimeout,
        document: { getElementById: element, createElement: () => ({ textContent: '' }) },
        window: { addEventListener() {}, location: { host: '127.0.0.1:8767' } },
        WebSocket: { OPEN: 1 }, Option: function () {}, alert() {},
        fakeContext: ctx, fakeSocket: { readyState: 1, send: value => sent.push(JSON.parse(value)) },
    });
    vm.runInContext(script + '\naudioContext = fakeContext; ws = fakeSocket;', sandbox);
    const run = code => vm.runInContext(code, sandbox);
    const control = msg => { sandbox.msg = msg; return run('handleControlMessage(msg)'); };
    const chunk = bytes => { sandbox.bytes = bytes; return run('handleAudioChunk(bytes)'); };
    const begin = id => {
        sandbox.id = id;
        run('currentRequestId = id');
        control({ type: 'stream_start', request_id: id, sampleRate: 24000, format: 'wav' });
    };
    return { nodes, element, ctx, sent, run, control, chunk, begin };
}

test('received PCM fragments are all scheduled before previous onended, with no gap', async () => {
    const f = fixture(); f.begin('a');
    const samples = Array.from({ length: 7200 }, (_, i) => (i % 100) * 100 - 5000);
    for (let i = 0; i < 3; i++) await f.chunk(wav(samples));
    assert.equal(f.nodes.length, 3, 'schedule ahead; do not wait for ended callbacks');
    for (let i = 1; i < f.nodes.length; i++) {
        assert.ok(Math.abs(f.nodes[i].when - f.nodes[i - 1].when - 0.3) < 1e-10);
    }
    assert.ok(f.nodes[0].when > f.ctx.currentTime, 'reserve startup buffering');
});

test('WAV containers are removed and original signed PCM is preserved exactly', async () => {
    const f = fixture(); f.begin('a');
    await f.chunk(wav([-32768, -3130, -3111, 0, 32767]));
    assert.equal(f.nodes.length, 1);
    const actual = Array.from(f.nodes[0].buffer.getChannelData(0));
    assert.deepEqual(actual, [-1, -3130 / 32768, -3111 / 32768, 0, 32767 / 32768]);
    assert.equal(f.nodes[0].buffer.sampleRate, 24000);
});

test('unsupported context rate combines PCM and resamples a single buffer', async () => {
    const f = fixture(44100); f.begin('a');
    await f.chunk(wav([100, 200])); await f.chunk(wav([300, 400]));
    assert.equal(f.nodes.length, 0, 'avoid independent per-fragment resampling');
    await f.control({ type: 'stream_end', request_id: 'a', total_chunks: 2 });
    assert.equal(f.nodes.length, 1);
    assert.equal(f.nodes[0].buffer.sampleRate, 24000);
    assert.deepEqual(Array.from(f.nodes[0].buffer.getChannelData(0)), [100, 200, 300, 400].map(x => x / 32768));
});

test('new requests stop old nodes and discard late frames and complete responses', async () => {
    const f = fixture(); f.begin('old'); await f.chunk(wav([100, 200]));
    const old = f.nodes[0];
    await f.run('streamSynthesize()');
    assert.equal(old.stopped, true);
    const id = f.sent.at(-1).request_id;
    await f.chunk(wav([999]));
    f.control({ type: 'stream_end', request_id: 'old', total_chunks: 2 });
    assert.equal(f.nodes.length, 1);
    f.control({ type: 'audio', request_id: 'old', data: '' });
    assert.equal(f.element('audioPlayer').paused, true);
    f.begin(id); await f.chunk(wav([300]));
    assert.equal(f.nodes.length, 2);
    assert.deepEqual(Array.from(f.nodes[1].buffer.getChannelData(0)), [300 / 32768]);
});

test('stream_end does not report playback complete until scheduled nodes end', async () => {
    const f = fixture(); f.begin('a'); await f.chunk(wav([100, 200]));
    f.control({ type: 'stream_end', request_id: 'a', total_chunks: 1 });
    assert.equal(f.element('status').textContent, '正在播放...');
    f.nodes[0].onended();
    assert.equal(f.element('status').textContent, '已连接');
});

test('truncated WAV produces a playback error instead of scheduling corrupt audio', async () => {
    const f = fixture(); f.begin('a');
    await f.chunk(wav([1, 2]).slice(0, 55));
    assert.equal(f.nodes.length, 0);
    assert.ok(f.element('log').children.some(item => /音频.*失败/.test(item.textContent)));
});
