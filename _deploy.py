import paramiko, os

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.31.128', username='zyys', password='606808')
sftp = ssh.open_sftp()

DIST = r'\\192.168.31.28\zyys\letta\Mneme3\mneme\web\dist'
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

ssh.exec_command('echo 606808 | sudo -S rm -rf /var/www/mneme/*')
ssh.exec_command('echo 606808 | sudo -S cp -r /home/zyys/mneme_fe/* /var/www/mneme/')

_, out, _ = ssh.exec_command('ls /var/www/mneme/assets/KnowledgeRedesign*')
print(out.read().decode().strip())
ssh.close()
print('Done - Ctrl+Shift+R')
