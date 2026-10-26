#!/usr/bin/env python3
"""Send real Mac telemetry over USB, or authenticated LAN HTTP."""
import argparse, base64, json, os, pathlib, socket, time, urllib.request, subprocess, math, plistlib
import psutil

_disk_info_cache = {}

def collect_disks():
    # Real mounted volumes only: omit recovery volumes and virtual disk images.
    partitions = sorted(psutil.disk_partitions(), key=lambda p: p.mountpoint)
    active = {p.mountpoint: p.device for p in partitions}
    for path in list(_disk_info_cache):
        if active.get(path) != _disk_info_cache[path][0]:
            del _disk_info_cache[path]
    candidates = [('/System/Volumes/Data', 'Mac SSD')]
    for part in partitions:
        path = part.mountpoint
        if not path.startswith('/Volumes/') or not part.device.startswith('/dev/disk'):
            continue
        if path not in _disk_info_cache:
            try:
                raw = subprocess.check_output(['/usr/sbin/diskutil', 'info', '-plist', path], timeout=2)
                _disk_info_cache[path] = (part.device, plistlib.loads(raw))
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
        info = _disk_info_cache[path][1]
        if info.get('Internal') is not False or info.get('BusProtocol') == 'Disk Image':
            continue
        candidates.append((path, info.get('VolumeName') or pathlib.Path(path).name))
    disks = []
    for path, name in candidates:
        if any(d['mountpoint'] == path for d in disks):
            continue
        try:
            usage = psutil.disk_usage(path)
        except OSError:
            continue  # A volume can disappear between discovery and sampling.
        disks.append(dict(name=name, mountpoint=path, free_gb=usage.free/1024**3,
                          total_gb=usage.total/1024**3, used_percent=usage.percent))
        if len(disks) >= 3:
            break
    return disks

def collect():
    memory = psutil.virtual_memory()
    disks = collect_disks()
    telemetry = dict(cmd='stats',host=socket.gethostname().split('.')[0],
                cpu=psutil.cpu_percent(interval=.2),memory_percent=memory.percent,
                memory_used_gb=(memory.total-memory.available)/1024**3,
                memory_total_gb=memory.total/1024**3,disks=disks,
                uptime_hours=(time.time()-psutil.boot_time())/3600)
    try:
        result=subprocess.run(['/opt/homebrew/bin/macmon','pipe','-s','1','-i','500'],
                              capture_output=True,text=True,timeout=4,check=True)
        sensors=json.loads(result.stdout.splitlines()[-1])
        for field,source in [('cpu_temp_c','cpu_temp_avg'),('gpu_temp_c','gpu_temp_avg')]:
            value=sensors.get('temp',{}).get(source)
            if isinstance(value,(int,float)) and math.isfinite(value) and 1<value<130:
                telemetry[field]=round(value,1)
        telemetry['fans']=[dict(name=f['name'],rpm=f['rpm']) for f in sensors.get('fans',[])
                           if isinstance(f.get('rpm'),(int,float)) and f['rpm']>=0]
    except (OSError,subprocess.SubprocessError,ValueError,KeyError):
        telemetry['sensor_status']='unavailable'
    return telemetry

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--serial')
    parser.add_argument('--url')
    parser.add_argument('--token-file',default='private/ota-token')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if not args.serial and not args.url:
        parser.error('Specify --serial or --url')
    port=None
    if args.serial:
        import serial
        port=serial.Serial()
        port.port=args.serial;port.baudrate=115200;port.timeout=.2
        port.dtr=False;port.rts=False;port.open()
    try:
        while True:
            data=json.dumps(collect()).encode()
            if port:
                port.write(data+b'\n')
            else:
                token=pathlib.Path(args.token_file).read_text().strip()
                auth=base64.b64encode(('admin:'+token).encode()).decode()
                req=urllib.request.Request(args.url.rstrip('/')+'/stats',data=data,
                      headers={'Content-Type':'application/json','Authorization':'Basic '+auth})
                with urllib.request.urlopen(req,timeout=5) as response:
                    if response.status!=200:raise RuntimeError('Telemetry rejected')
            if args.once:break
            time.sleep(4)
    finally:
        if port:port.close()
if __name__=='__main__':main()
