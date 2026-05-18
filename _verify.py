import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.31.128', username='zyys', password='606808')

_, out, _ = ssh.exec_command('cat /var/www/mneme/index.html')
html = out.read().decode()
print('index:', 'KnowledgeRedesign-ZnaH-G-W' in html)
print('Size:', len(html))

_, out, _ = ssh.exec_command('ls /var/www/mneme/assets/KnowledgeRedesign*')
print(out.read().decode().strip())

ssh.close()
