# Deploy frontend dist to 192.168.31.128
# Run this from PowerShell on the server or any machine with SSH access

$distPath = "\\192.168.31.28\zyys\letta\Mneme3\mneme\web\dist\*"
$target = "zyys@192.168.31.128"
$targetPath = "/var/www/mneme/"

Write-Host "Deploying frontend to $target ..."

# Use SCP to copy dist files
scp -r -o StrictHostKeyChecking=no $distPath "${target}:${targetPath}" 2>&1

if ($LASTEXITCODE -eq 0) {
    Write-Host "Dist copied. Reloading nginx..."
    ssh -o StrictHostKeyChecking=no $target "nginx -s reload" 2>&1
    Write-Host "Deploy complete. Visit http://192.168.31.128:5280/app/knowledge-v2"
} else {
    Write-Host "SCP failed. Trying rsync..."
    rsync -av --delete "\\192.168.31.28\zyys\letta\Mneme3\mneme\web\dist/" "${target}:/var/www/mneme/"
}
