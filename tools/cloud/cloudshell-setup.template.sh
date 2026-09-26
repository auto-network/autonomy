# Autonomy AWS test access (tools/cloud/AWS-RUNBOOK.md). Template: the agent
# replaces @HANDOFF_PUBLIC_KEY@ with a one-time RSA key it holds in memory.
# The operator uploads the result to CloudShell and runs: bash autonomy-setup.sh
set -e
R=us-west-2; U=autonomy-test-agent; ROLE=autonomy-test-ssm
cat > /tmp/pol.json <<'P'
{"Version":"2012-10-17","Statement":[
{"Sid":"Ec2SsmOneRegion","Effect":"Allow","Action":["ec2:*","ssm:*","ssmmessages:*","ec2messages:*"],"Resource":"*","Condition":{"StringEquals":{"aws:RequestedRegion":"us-west-2"}}},
{"Sid":"TestSizesOnly","Effect":"Deny","Action":"ec2:RunInstances","Resource":"arn:aws:ec2:*:*:instance/*","Condition":{"StringNotEquals":{"ec2:InstanceType":["m7i.xlarge","m7i.2xlarge","m8i.xlarge","m8i.2xlarge"]}}},
{"Sid":"PassTestRole","Effect":"Allow","Action":"iam:PassRole","Resource":"arn:aws:iam::*:role/autonomy-test-ssm"},
{"Sid":"Budgets","Effect":"Allow","Action":["budgets:ViewBudget","budgets:ModifyBudget"],"Resource":"*"}]}
P
cat > /tmp/trust.json <<'P'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
P
aws iam create-role --role-name $ROLE --assume-role-policy-document file:///tmp/trust.json >/dev/null 2>&1 || true
aws iam attach-role-policy --role-name $ROLE --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name $ROLE >/dev/null 2>&1 || true
aws iam add-role-to-instance-profile --instance-profile-name $ROLE --role-name $ROLE >/dev/null 2>&1 || true
aws iam create-user --user-name $U >/dev/null 2>&1 || true
aws iam put-user-policy --user-name $U --policy-name autonomy-test --policy-document file:///tmp/pol.json
K=$(aws iam create-access-key --user-name $U --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
A=$(aws sts get-caller-identity --query Account --output text)
cat > /tmp/pub.pem <<'P'
@HANDOFF_PUBLIC_KEY@
P
printf '{"aws_access_key_id":"%s","aws_secret_access_key":"%s","region":"%s","account":"%s"}' $(echo $K | cut -d' ' -f1) $(echo $K | cut -d' ' -f2) $R $A \
 | openssl pkeyutl -encrypt -pubin -inkey /tmp/pub.pem -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 | base64 -w0 > ~/autonomy-key.enc
unset K
rm -f /tmp/pol.json /tmp/trust.json /tmp/pub.pem
# The file holds only ciphertext that Claude's session alone can open.
URL=$(curl -s --max-time 20 --data-binary @$HOME/autonomy-key.enc https://paste.rs || true)
echo
if [ -n "$URL" ]; then
  echo "=============================================="
  echo "  Done. Tell Claude this code:  ${URL##*/}"
  echo "=============================================="
else
  echo "Done, but the upload failed. Use Actions > Download file: autonomy-key.enc"
fi
