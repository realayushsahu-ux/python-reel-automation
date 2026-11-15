$ErrorActionPreference = 'Stop'

$workerDir = $PSScriptRoot
$projectRoot = Split-Path -Parent $workerDir
$envPath = Join-Path $projectRoot '.env'
$configPath = Join-Path $workerDir 'wrangler.toml'
$exampleConfigPath = Join-Path $workerDir 'wrangler.example.toml'

if (-not (Test-Path -LiteralPath $configPath)) {
    if (-not (Test-Path -LiteralPath $exampleConfigPath)) {
        throw 'Cloudflare config template is missing: wrangler.example.toml'
    }
    Copy-Item -LiteralPath $exampleConfigPath -Destination $configPath
    Write-Host 'Created a local Wrangler config from the public template.'
}

if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Project .env file not found: $envPath"
}
if (-not (Get-Command node.exe -ErrorAction SilentlyContinue) -or
    -not (Get-Command npx.cmd -ErrorAction SilentlyContinue)) {
    throw 'Node.js and npm/npx are required. Install Node.js, then run this script again.'
}

function Get-DotEnvValue([string]$key) {
    $pattern = '^\s*' + [regex]::Escape($key) + '=(.*)$'
    foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
        if ($line -match $pattern) {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ''
}

function Set-DotEnvValue([string]$key, [string]$value) {
    $lines = [System.Collections.Generic.List[string]]::new()
    $pattern = '^\s*' + [regex]::Escape($key) + '='
    $found = $false
    foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
        if ($line -match $pattern) {
            $lines.Add("$key=$value")
            $found = $true
        } else {
            $lines.Add($line)
        }
    }
    if (-not $found) { $lines.Add("$key=$value") }
    [System.IO.File]::WriteAllLines($envPath, $lines, [System.Text.UTF8Encoding]::new($false))
}

function Invoke-Wrangler([string[]]$arguments) {
    & npx.cmd --yes wrangler@latest @arguments
    if ($LASTEXITCODE -ne 0) { throw "Wrangler command failed: $($arguments -join ' ')" }
}

function Put-WorkerSecret([string]$name, [string]$value) {
    $value | & npx.cmd --yes wrangler@latest secret put $name --config $configPath
    if ($LASTEXITCODE -ne 0) { throw "Could not store Worker secret $name" }
}

$appSecret = Get-DotEnvValue 'META_APP_SECRET'
$verifyToken = Get-DotEnvValue 'META_WEBHOOK_VERIFY_TOKEN'
if (-not $appSecret -or -not $verifyToken) {
    throw 'Set META_APP_SECRET and META_WEBHOOK_VERIFY_TOKEN in the project .env first.'
}

$relayToken = Get-DotEnvValue 'WEBHOOK_RELAY_TOKEN'
if (-not $relayToken) {
    $randomBytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($randomBytes) } finally { $rng.Dispose() }
    $relayToken = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    Set-DotEnvValue 'WEBHOOK_RELAY_TOKEN' $relayToken
}

Push-Location $workerDir
try {
    $null = & npx.cmd --yes wrangler@latest whoami 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'A browser will open so you can sign in to Cloudflare.'
        Invoke-Wrangler @('login')
    }

    $toml = [System.IO.File]::ReadAllText($configPath)
    if ($toml -notmatch 'binding\s*=\s*"REEL_EVENTS"') {
        Write-Host 'Creating the free Workers KV event queue...'
        Invoke-Wrangler @('kv', 'namespace', 'create', 'REEL_EVENTS', '--binding', 'REEL_EVENTS', '--update-config', '--config', $configPath)
    }

    Write-Host 'Creating the Worker endpoint...'
    Invoke-Wrangler @('deploy', '--config', $configPath)

    Write-Host 'Saving Meta and relay secrets to Cloudflare (secret values will not be printed)...'
    Put-WorkerSecret 'META_APP_SECRET' $appSecret
    Put-WorkerSecret 'META_WEBHOOK_VERIFY_TOKEN' $verifyToken
    Put-WorkerSecret 'RELAY_TOKEN' $relayToken

    Write-Host 'Publishing the secured Worker...'
    $deployLines = & npx.cmd --yes wrangler@latest deploy --config $configPath 2>&1
    $deployLines | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw 'Final Worker deployment failed.' }
    $deployText = $deployLines -join "`n"
    $workerUrl = [regex]::Match($deployText, 'https://[a-zA-Z0-9-]+\.[a-zA-Z0-9-]+\.workers\.dev').Value
    if (-not $workerUrl) {
        Write-Host 'Deployment completed, but the URL was not detected automatically.'
        Write-Host 'Copy the https://...workers.dev URL shown above and set WEBHOOK_RELAY_URL in .env.'
        exit 0
    }
    Set-DotEnvValue 'WEBHOOK_RELAY_URL' $workerUrl
    Write-Host ''
    Write-Host 'Stable webhook relay is deployed.'
    Write-Host "Worker URL: $workerUrl"
    Write-Host "Meta callback URL: $workerUrl/meta/webhook"
    Write-Host 'Next, enter that callback URL in Meta and verify it using the existing webhook verify token.'
} finally {
    Pop-Location
}
