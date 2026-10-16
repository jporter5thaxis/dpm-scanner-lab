"""Matched moving 10x10-cell fixtures; synthetic timing, not physical DPM claims."""
import argparse
import importlib.util
import json
from pathlib import Path
import platform
import sys
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import Settings
from backend.vision import Pipeline
from scripts.synthetic_data_matrix import scene

parser = argparse.ArgumentParser()
parser.add_argument('--baseline', type=Path)
parser.add_argument('--output', type=Path, default=Path('docs/data-matrix-benchmark.json'))
args = parser.parse_args()
implementations = [('optimized', Pipeline, Settings())]
if args.baseline:
    spec = importlib.util.spec_from_file_location('backend.vision_before', args.baseline)
    old = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old
    spec.loader.exec_module(old)
    implementations.insert(0, ('previous', old.Pipeline,
                               Settings(processing_fps=12, decoder_budget_ms=60, search_interval=2)))
report = {'scope': 'SYNTHETIC 10x10 ECC200, 1920x1080; no engraved toolholder reliability claim',
          'host': platform.platform(), 'results': {}}
for name, factory, cfg in implementations:
    samples, rows = [], []
    for module in (3, 5):
        for rotation in (0, 90, 180, 270, 27):
            for condition in ('clean', 'inverted', 'low_contrast', 'blur'):
                pipeline = factory()
                reads, false_values, paths, times = 0, [], [], []
                for frame in range(6):
                    image = scene(x=1300+frame*9, y=350+frame*4, module=module,
                                  rotation=rotation, condition=condition, width=1920, height=1080)
                    result = pipeline.process(image, cfg, timestamp=frame/24)
                    values = [r['value'] for r in result['decoded']]
                    reads += '123456' in values
                    false_values.extend(v for v in values if v != '123456')
                    paths.extend(r['pipeline'] for r in result['decoded'])
                    times.append(result['processing_ms'])
                samples.extend(times)
                rows.append({'module_px': module, 'rotation': rotation, 'condition': condition,
                             'read_frames': reads, 'frames': 6, 'false_values': false_values,
                             'processing_ms': times, 'paths': paths})
    result = {'cases': len(rows), 'frames': len(samples), 'read_frames': sum(r['read_frames'] for r in rows),
              'false_reads': sum(len(r['false_values']) for r in rows),
              'mean_ms': round(float(np.mean(samples)), 2), 'p95_ms': round(float(np.percentile(samples, 95)), 2),
              'settings': cfg.model_dump(), 'trials': rows}
    report['results'][name] = result
    print(name, json.dumps({k: v for k, v in result.items() if k not in ('trials', 'settings')}), flush=True)
args.output.write_text(json.dumps(report, indent=2)+'\n')
