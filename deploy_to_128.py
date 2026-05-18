"""Deploy frontend dist/ to 192.168.31.128:/var/www/mneme/ via SCP."""
import paramiko
import os
import glob

HOST = '192.168.31.128'
USER = 'zyys'
PASSWORD = '606808'
DIST = r'\\192.168.31.28\zyys\letta\Mneme3\mneme\web\dist'
TARGET = '/var/www/mneme'

def main():
    print(f'Connecting to {HOST}...')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=15)

    sftp = ssh.open_sftp()

    # Remove old files
    print('Cleaning old files...')
    try:
        for item in sftp.listdir(TARGET):
            item_path = f'{TARGET}/{item}'
            try:
                sftp.remove(item_path)
            except IOError:
                # Directory - remove recursively
                _rmdir(sftp, item_path)
    except Exception as e:
        print(f'  (clean warning: {e})')

    # Upload new files
    print(f'Uploading from {DIST}...')
    _upload_dir(sftp, DIST, TARGET)

    sftp.close()

    # Reload nginx
    print('Reloading nginx...')
    stdin, stdout, stderr = ssh.exec_command('nginx -s reload 2>&1')
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out or err:
        print(f'  {out}{err}')

    ssh.close()
    print(f'Done! Visit http://{HOST}:5280/app/knowledge-v2')

def _upload_dir(sftp, local_dir, remote_dir):
    for item in os.listdir(local_dir):
        local_path = os.path.join(local_dir, item)
        remote_path = f'{remote_dir}/{item}'
        if os.path.isfile(local_path):
            sftp.put(local_path, remote_path)
        elif os.path.isdir(local_path):
            try:
                sftp.mkdir(remote_path)
            except IOError:
                pass
            _upload_dir(sftp, local_path, remote_path)

def _rmdir(sftp, path):
    for item in sftp.listdir(path):
        item_path = f'{path}/{item}'
        try:
            sftp.remove(item_path)
        except IOError:
            _rmdir(sftp, item_path)
    sftp.rmdir(path)

if __name__ == '__main__':
    main()
