import paramiko, os, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.31.128', username='zyys', password='606808')
sftp = ssh.open_sftp()

DIST = r'\\192.168.31.28\zyys\letta\Mneme3\mneme\web\dist'

# Upload to /home/zyys/mneme_fe/
ssh.exec_command('rm -rf /home/zyys/mneme_fe && mkdir -p /home/zyys/mneme_fe/assets')
for root, dirs, files in os.walk(DIST):
    for f in files:
        local = os.path.join(root, f)
        rel = os.path.relpath(local, DIST).replace('\\', '/')
        remote = f'/home/zyys/mneme_fe/{rel}'
        rd = os.path.dirname(remote)
        try: sftp.mkdir(rd)
        except IOError: pass
        sftp.put(local, remote)
sftp.close()

# Verify staging
_, out, _ = ssh.exec_command('wc -c < /home/zyys/mneme_fe/index.html')
size = out.read().decode().strip()
print(f'Staged index.html: {size} bytes')

# Try different sudo approaches
print('Trying sudo cp...')
stdin, stdout, stderr = ssh.exec_command('echo 606808 | sudo -S sh -c "rm -rf /var/www/mneme/*; cp -r /home/zyys/mneme_fe/* /var/www/mneme/" 2>&1')
err = stderr.read().decode()
out = stdout.read().decode()
if err: print('stderr:', err[:300])
if out: print('stdout:', out[:300])

time.sleep(0.5)

# Verify
_, out, _ = ssh.exec_command('wc -c < /var/www/mneme/index.html')
result = out.read().decode().strip()
print(f'Result index.html: {result} bytes')

if result == '0' or not result:
    print('FAILED - trying alternate method...')
    # Method 2: copy file by file
    _, out, _ = ssh.exec_command("echo 606808 | sudo -S cp /home/zyys/mneme_fe/index.html /var/www/mneme/index.html 2>&1")
    err = out.read().decode()
    print('cp index:', err[:200] if err else 'OK?')

    _, out, _ = ssh.exec_command('wc -c < /var/www/mneme/index.html')
    print('After direct cp:', out.read().decode().strip(), 'bytes')

ssh.close()
