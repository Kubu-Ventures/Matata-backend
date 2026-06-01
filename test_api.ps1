# Get token
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/auth/anonymous" -Method POST
$token = $resp.session_token
Write-Host "Token: $token"

# Build multipart body
$boundary = [System.Guid]::NewGuid().ToString()
$metadata = '{"crisis_type":"flood","infrastructure_type":"residential","damage_severity":"partial","lat":1.234,"lng":36.789}'

$bodyLines = @(
    "--$boundary",
    "Content-Disposition: form-data; name=`"metadata`"",
    "",
    $metadata,
    "--$boundary--"
)
$bodyText = $bodyLines -join "`r`n"

$headers = @{
    Authorization = "Bearer $token"
    "Content-Type" = "multipart/form-data; boundary=$boundary"
}

# Submit report
$result = Invoke-RestMethod `
    -Uri "http://localhost:8000/api/v1/reports" `
    -Method POST `
    -Headers $headers `
    -Body $bodyText

$result | ConvertTo-Json