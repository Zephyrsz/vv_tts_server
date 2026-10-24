import gc
import io
import json
import pathlib
import re
import resource
import subprocess
import sys
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from local_engine import LocalVoicevoxEngine


def rss():
    return int(subprocess.check_output(['ps', '-o', 'rss=', '-p', str(__import__('os').getpid())]))/1024


def run(mode):
    text = json.loads(pathlib.Path(__file__).with_name('macos_baseline.json').read_text())['texts']['long']
    engine = LocalVoicevoxEngine(ROOT/'assets', ROOT/'assets/models', 4)
    result = {'mode': mode, 'rss_init_mib': rss(), 'chunks': []}
    t = time.perf_counter()
    parts = re.findall(r'[^。]+。?', text) if mode == 'sentences' else [text]
    for part in parts:
        audio = engine.synthesize(part, 0, {})
        with wave.open(io.BytesIO(audio)) as w:
            duration = w.getnframes()/w.getframerate()
        result['chunks'].append({'characters': len(part), 'duration_s': duration, 'rss_mib': rss(),
                                 'elapsed_since_start_ms': (time.perf_counter()-t)*1000})
        del audio
    result['total_ms'] = (time.perf_counter()-t)*1000
    result['peak_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1048576
    result['rss_before_unload_mib'] = rss()
    with __import__('voicevox_core.blocking', fromlist=['VoiceModelFile']).VoiceModelFile.open(ROOT/'assets/models/0.vvm') as m:
        model_id = m.id
    engine.synthesizer.unload_voice_model(model_id)
    gc.collect()
    result['rss_after_unload_mib'] = rss()
    engine.synthesizer.close()
    gc.collect()
    result['rss_after_synth_close_mib'] = rss()
    return result


if __name__ == '__main__':
    if len(sys.argv) > 1:
        print(json.dumps(run(sys.argv[1])))
    else:
        results = []
        for mode in ['whole', 'sentences']:
            p = subprocess.run([sys.executable, __file__, mode], capture_output=True, text=True, check=True)
            results.append(json.loads(p.stdout))
        pathlib.Path(__file__).with_name('single_model_memory.json').write_text(json.dumps(results, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(results, ensure_ascii=False, indent=2))
