#!/usr/bin/env python3
"""Run only after the user approves preview and the compatible firmware is deployed."""
import argparse,datetime,os,pathlib,shutil,subprocess
p=argparse.ArgumentParser();p.add_argument('--preview-approved',action='store_true');a=p.parse_args()
if not a.preview_approved:p.error('Preview approval is required before replacing the installed agent')
root=pathlib.Path(__file__).resolve().parent.parent
runtime=pathlib.Path.home()/'Library/Application Support/MacStatusBoard'
if not (runtime/'.venv/bin/python').exists():raise SystemExit('Existing runtime missing; install psutil in its private venv first')
backup=runtime/'recovery'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S');backup.mkdir(parents=True,mode=0o700)
for name in ['run_agent.py','status_agent.py','dashboard_sources.py','telemetry_server.py']:
    target=runtime/'host'/name
    if target.exists():shutil.copy2(target,backup/name)
    shutil.copy2(root/'host'/name,target)
subprocess.run(['launchctl','kickstart','-k','gui/%d/com.local.status-board'%os.getuid()],check=True)
print('Agent installed; recovery:',backup)
