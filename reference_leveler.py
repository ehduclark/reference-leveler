#!/usr/bin/env python3
"""Reference Leveler: reference-driven batch loudness normalizer."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import csv

EXTENSIONS = {'.wav','.aiff','.aif','.flac','.alac','.mp3','.m4a','.aac','.ogg','.opus'}
LOSSLESS = {'wav','aiff','flac','alac','pcm_s16le','pcm_s24le','pcm_s32le','pcm_f32le','pcm_f64le'}

def run(cmd):
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-1600:])
    return p.stdout, p.stderr

def probe(path):
    out,_ = run(['ffprobe','-v','error','-show_entries','stream=codec_name,codec_type,bit_rate,sample_rate,bits_per_raw_sample,bits_per_sample:format=bit_rate','-of','json',str(path)])
    data=json.loads(out)
    audio=next((s for s in data.get('streams',[]) if s.get('codec_type')=='audio'),None)
    if not audio: raise ValueError(f'No audio stream: {path}')
    return audio, data.get('format',{})

def measure(path):
    _,err=run(['ffmpeg','-hide_banner','-nostats','-i',str(path),'-map','0:a:0','-af','loudnorm=I=-24:TP=-1:LRA=11:print_format=json','-f','null','-'])
    match=re.search(r'\{\s*"input_i".*?\}',err,re.S)
    if not match: raise ValueError(f'No loudness measurements: {path}')
    d=json.loads(match.group())
    result={k:float(d[k]) for k in ('input_i','input_tp','input_lra','input_thresh')}
    if not all(math.isfinite(v) for v in result.values()): raise ValueError(f'Invalid loudness measurements: {path}')
    return result

def canonical(p): return Path(p).expanduser().resolve()

def separate(source,output):
    if source==output or source in output.parents or output in source.parents:
        raise ValueError('Source and output must be separate, non-overlapping directory trees')

def output_info(path):
    audio,fmt=probe(path)
    codec=audio['codec_name']
    if codec in LOSSLESS:
        return '.flac',['-c:a','flac'],codec
    rate=int(audio.get('bit_rate') or fmt.get('bit_rate') or 0)
    rate=max(64000,min(rate,512000)) if rate else 192000
    if codec=='mp3': return '.mp3',['-c:a','libmp3lame','-b:a',str(rate)],codec
    if codec=='aac': return '.m4a',['-c:a','aac','-b:a',str(rate)],codec
    if codec=='vorbis': return '.ogg',['-c:a','libvorbis','-b:a',str(rate)],codec
    if codec=='opus': return '.opus',['-c:a','libopus','-b:a',str(rate)],codec
    raise ValueError(f'Unsupported audio codec: {codec}')

def collect(source,output):
    paths=sorted(p for p in source.rglob('*') if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.is_symlink())
    plan=[]; targets={}
    for p in paths:
        ext,enc,codec=output_info(p)
        dest=output / p.relative_to(source).with_suffix(ext)
        key=str(dest).casefold()
        if key in targets: raise ValueError(f'Output collision: {p} and {targets[key]} -> {dest}')
        targets[key]=p
        plan.append((p,dest,enc,codec))
    return plan

def profile_cmd(args):
    p=canonical(args.reference)
    m=measure(p)
    target=m['input_i']
    if not -70 <= target <= -5: raise ValueError(f'Reference LUFS {target} outside supported range -70 to -5')
    profile={'schema':1,'reference':str(p),'target_lufs':target,'reference_true_peak':m['input_tp'],'reference_lra':m['input_lra'],'ceiling_dbtp':-1.0}
    dest=canonical(args.output)
    if dest.exists() and not args.force: raise ValueError(f'Profile exists: {dest}; use --force to replace')
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(profile,indent=2)+'\n')
    print(json.dumps(profile,indent=2))

def batch_cmd(args):
    raise RuntimeError(
        "Legacy batch processing has been retired for source safety. "
        "Use the verified renderer and forthcoming project-based "
        "batch orchestrator instead."
    )

    source=canonical(args.source)
    if not source.is_dir(): raise ValueError(f'Source directory missing: {source}')
    output=canonical(args.output) if args.output else canonical(input('Output directory (separate from source): ').strip())
    separate(source,output)
    if not args.profile: raise ValueError('Provide --profile')
    prof=json.loads(canonical(args.profile).read_text())
    if prof.get('schema')!=1: raise ValueError('Unsupported profile schema')
    target=float(prof['target_lufs']); ceiling=float(prof['ceiling_dbtp'])
    if not (-70 <= target <= -5 and -9 <= ceiling <= 0): raise ValueError('Invalid profile target or ceiling')
    plan=collect(source,output)
    print(f'Files: {len(plan)} | target: {target:.2f} LUFS | ceiling: {ceiling:.1f} dBTP')
    print(f'Source: {source}\nOutput: {output}')
    if args.dry_run:
        for p,d,_,_ in plan: print(f'{p.relative_to(source)} -> {d.relative_to(output)}')
        return
    if not args.yes and input('Proceed with batch processing? [y/N] ').strip().lower()!='y':
        print('Cancelled.'); return
    output.mkdir(parents=True,exist_ok=True)
    rows=[]
    for idx,(p,d,enc,codec) in enumerate(plan,1):
        print(f'[{idx}/{len(plan)}] {p.relative_to(source)}',flush=True)
        row={'source':str(p),'output':str(d),'codec':codec,'target_lufs':target,'status':'FAILED','error':''}
        tmp=None
        try:
            if d.exists() and not args.overwrite:
                row['status']='SKIPPED_EXISTS'; rows.append(row); continue
            before=measure(p)
            row.update(before_lufs=before['input_i'],before_true_peak=before['input_tp'])
            # Use measured first pass. Dynamic normalization is intentional when linear cannot reach target.
            filt=(f'loudnorm=I={target}:TP={ceiling}:LRA=11:'
                  f'measured_I={before["input_i"]}:measured_TP={before["input_tp"]}:'
                  f'measured_LRA={before["input_lra"]}:measured_thresh={before["input_thresh"]}:'
                  'linear=false:print_format=summary')
            d.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(prefix='.reference-leveler-',suffix=d.suffix,dir=d.parent)
            os.close(fd); tmp=Path(name)
            run(['ffmpeg','-hide_banner','-nostdin','-y','-i',str(p),'-map','0:a:0','-map_metadata','0','-af',filt,*enc,str(tmp)])
            after=measure(tmp)
            row.update(after_lufs=after['input_i'],after_true_peak=after['input_tp'])
            row['status']='PASS' if abs(after['input_i']-target)<=0.5 and after['input_tp']<=ceiling+0.15 else 'REVIEW'
            os.replace(tmp,d)
        except Exception as exc:
            row['error']=str(exc)
            print(f'  ERROR: {exc}',file=sys.stderr)
        finally:
            if tmp and tmp.exists(): tmp.unlink()
        rows.append(row)
    report=output/'reference_leveler_report.csv'
    fields=['source','output','codec','target_lufs','before_lufs','after_lufs','before_true_peak','after_true_peak','status','error']
    with report.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    print(f'Report: {report}')
    print('Summary:',{status:sum(r['status']==status for r in rows) for status in sorted(set(r['status'] for r in rows))})

def main():
    ap=argparse.ArgumentParser(description='Reference-driven batch loudness matching')
    sub=ap.add_subparsers(dest='command',required=True)
    a=sub.add_parser('profile');a.add_argument('reference');a.add_argument('--output',required=True);a.add_argument('--force',action='store_true');a.set_defaults(func=profile_cmd)
    a=sub.add_parser('batch');a.add_argument('--source',required=True);a.add_argument('--output');a.add_argument('--profile',required=True);a.add_argument('--dry-run',action='store_true');a.add_argument('--yes',action='store_true');a.add_argument('--overwrite',action='store_true');a.set_defaults(func=batch_cmd)
    args=ap.parse_args()
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): ap.error('ffmpeg and ffprobe must be installed and on PATH')
    try: args.func(args)
    except (ValueError,RuntimeError,OSError,KeyError) as exc: ap.error(str(exc))
if __name__=='__main__': main()
