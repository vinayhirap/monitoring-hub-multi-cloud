$r = Invoke-RestMethod -Uri 'http://3.109.181.40/api/v1/label/__name__/values'
foreach ($x in $r.data) {
    if ($x -like '*ec2*') { Write-Host $x }
}