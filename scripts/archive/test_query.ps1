$start = (Get-Date).AddHours(-6).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$end = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$url = "http://3.109.181.40/api/v1/query_range?query=aws_ec2_network_in_average{dimension_InstanceId=`"i-0201a7f5db29d1317`"}&start=$start&end=$end&step=60s"
$r = Invoke-RestMethod -Uri $url
$r | ConvertTo-Json -Depth 10