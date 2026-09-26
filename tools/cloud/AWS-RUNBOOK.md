# AWS test infrastructure — runbook

Autonomy uses one AWS account for disposable test machines: clean Windows
hosts on which the published install is proven end to end (fresh machine →
signed images → running node). Account-specific facts (account number, alert
address, current resources, run log) live in the graph note `graph://eda7017f-d2f` (tags
`runbook,aws`); this file holds the procedure and the tools, which contain no
account data because the repository is public.

## What an agent holds

| Thing | Where |
|---|---|
| IAM user `autonomy-test-agent` access key | vault, org `autonomy`, tier **audited**, name `aws-ec2-test` — JSON `{aws_access_key_id, aws_secret_access_key, region, account}` |
| Permissions | inline policy `autonomy-test` (below): EC2 + Systems Manager in one region, four machine sizes, pass one role, budgets |
| Role for running commands inside machines | `autonomy-test-ssm` (+ instance profile of the same name), `AmazonSSMManagedInstanceCore` |
| Spending guard | AWS Budget `autonomy-test-monthly`, email at 80 % actual and 100 % forecast |

The key never touches `~/.aws`. `tools/cloud/aws-test` releases it from the
vault into the session's private ramfs, exports it to the environment, and
execs the AWS CLI:

```bash
tools/cloud/aws-test sts get-caller-identity
tools/cloud/aws-test ec2 describe-instances
```

The AWS CLI is not in the session image. Install it user-local (no root):

```bash
curl -sSfo /tmp/awscliv2.zip https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip
python3 -c "import zipfile;zipfile.ZipFile('/tmp/awscliv2.zip').extractall('/tmp/awscli')"
chmod -R +x /tmp/awscli/aws && /tmp/awscli/aws/install -i ~/.local/aws-cli -b ~/.local/bin --update
```

## Rules for every run

1. Tag everything created: `autonomy:purpose=install-test`, `autonomy:session=<tmux name>`.
2. Tear down what a run created before the run is reported (instances, volumes,
   snapshots that are not a named baseline, security groups, key pairs).
3. Only the four permitted sizes (`m7i.xlarge`, `m7i.2xlarge`, `m8i.xlarge`,
   `m8i.2xlarge`); the policy refuses the rest. These families support nested
   virtualization (needed for WSL2 inside Windows) on virtual instances since
   February 2026.
4. No inbound ports. Commands run inside machines through Systems Manager Run
   Command (`AWS-RunPowerShellScript`) using the `autonomy-test-ssm` role.
5. Record each run (what, cost estimate, result, teardown confirmed) in the
   graph runbook note.

## Granting access (first time or rotation) without the operator driving

Only the operator can sign in to the account, so the grant is one upload and
one short code:

1. The agent generates a one-time 4096-bit RSA key pair in `/dev/shm` (memory
   only) and fills `@HANDOFF_PUBLIC_KEY@` in `cloudshell-setup.template.sh`
   with the public half, producing `autonomy-setup.sh`.
2. The agent shares the file with `graph share` under an extension the session
   viewer offers as a download (for example `.run`).
3. The operator uploads it in AWS CloudShell (Actions → Upload file) and runs
   `bash autonomy-setup.run`. The script creates/updates the role, user and
   policy, mints an access key, encrypts `{key, region, account}` with the
   public key (RSA-OAEP-SHA256), posts only the ciphertext to paste.rs, and
   prints the paste code.
4. The operator sends the code. The agent fetches the paste, decrypts in
   memory, seals the JSON as `aws-ec2-test` (audited), checks the vault
   returns identical bytes, shreds the private key and plaintext, deletes the
   paste, and verifies with `aws-test sts get-caller-identity`.

Pitfall seen on the first grant: a phone keyboard turned the letter `O` into a
zero in the code; try the look-alike variants (`0/O`, `1/l/I`) before asking
again.

**Rotation:** repeat the grant (it mints a second key), verify, then delete the
old key with `aws iam delete-access-key` from CloudShell. A user holds at most
two keys.

**Revocation:** delete the IAM user `autonomy-test-agent` (or just its keys) in
the console. Nothing else in the account depends on it.

## Inline policy `autonomy-test`

```json
{"Version":"2012-10-17","Statement":[
 {"Sid":"Ec2SsmOneRegion","Effect":"Allow","Action":["ec2:*","ssm:*","ssmmessages:*","ec2messages:*"],"Resource":"*","Condition":{"StringEquals":{"aws:RequestedRegion":"us-west-2"}}},
 {"Sid":"TestSizesOnly","Effect":"Deny","Action":"ec2:RunInstances","Resource":"arn:aws:ec2:*:*:instance/*","Condition":{"StringNotEquals":{"ec2:InstanceType":["m7i.xlarge","m7i.2xlarge","m8i.xlarge","m8i.2xlarge"]}}},
 {"Sid":"PassTestRole","Effect":"Allow","Action":"iam:PassRole","Resource":"arn:aws:iam::*:role/autonomy-test-ssm"},
 {"Sid":"Budgets","Effect":"Allow","Action":["budgets:ViewBudget","budgets:ModifyBudget"],"Resource":"*"}]}
```

## Windows install test

### Baseline image (built and proven 2026-09-26)

`autonomy-win-baseline-2026-09-26`: Windows Server 2025 Datacenter (license
included) + WSL 2.7.14 from Microsoft's signed MSI, the Virtual Machine
Platform and WSL features, `hypervisorlaunchtype auto`, and a local
administrator `tester` (password in the vault, audited, `aws-win-tester`). No
Linux distribution, so each run starts as a Windows user with WSL enabled.
A launch from it is SSM-online in about 110 s; `wsl --install -d
Ubuntu-24.04` then has Ubuntu running (systemd PID 1, `/dev/kvm` present) in
about 95 s.

How it was built, and the traps on the way:

1. Launch the AWS Windows Server 2025 image on `m7i.xlarge` with
   `--cpu-options NestedVirtualization=enabled`, the `autonomy-test-ssm`
   instance profile, the `autonomy-test-noinbound` security group, a 100 GB gp3
   root volume and `HttpTokens=required`.
2. **SSM runs as LocalSystem, and WSL refuses it**
   (`Wsl/WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`); `wsl --install --no-distribution`
   as SYSTEM just prints "not installed". So: enable the features with
   `Enable-WindowsOptionalFeature` (Microsoft-Windows-Subsystem-Linux and
   VirtualMachinePlatform), install `wsl.<ver>.x64.msi` from the WSL GitHub
   release after checking its Authenticode signature, and run every WSL
   command as a real user. `tools/cloud/win-run-user` does that through a
   one-shot scheduled task running as `tester`.
3. **Reboots:** a `shutdown /r` started from an SSM command does not reliably
   happen. Exit the script with code 3010, and the SSM agent reboots the
   machine; the command then reports TimedOut, which is expected. Check
   `LastBootUpTime` and the CBS `RebootPending` key rather than trusting the
   SSM ping, which stays Online across a pending reboot.
4. **Two reboots were needed:** the first after enabling the features, the
   second (after `bcdedit /set hypervisorlaunchtype auto`) before
   `wsl --status` stopped reporting "virtualization is not enabled".
5. Unregister any distribution, delete `C:\autonomy-test\job-*`, then
   `create-image` (it reboots the instance; about 10–15 minutes to available),
   and terminate the build instance.

Tools: `tools/cloud/win-run <instance> <ps1|->` runs as SYSTEM through SSM;
`tools/cloud/win-run-user` runs as `tester`. Both print the script output and
the final status.

### Each run

Launch from the baseline, then as `tester` (`win-run-user`): install Ubuntu
24.04 under WSL, copy `deploy/install-published.sh` and a signed lock onto the
machine, and run the installer as root inside WSL with `--install-docker
--yes`. Check `/api/ping` from the Windows side with `curl.exe`, take Edge
headless screenshots, pull them back in 20,000-character base64 slices (SSM
returns at most about 24,000 characters of output per command), terminate.

**Run 1, 2026-09-26, release `2026.09.26-0d46057` (deploy/releases/):**

| Step | Time |
|---|---|
| Launch from baseline to SSM online | 99 s |
| Ubuntu 24.04 installed under WSL | 43 s |
| Docker Engine installed (apt from docker.com) | 43 s |
| cosign fetched, four signatures verified | 6 s |
| Four images pulled (about 15 GB unpacked) | 82 s |
| Subnet preflight, compose up, first 200 from `/api/ping` | 30 s |
| **Installer total** | **161 s** |

Windows side: `https://localhost:8080/api/ping` 200 and `/` 307 to `/welcome`.
Edge shows "Your connection isn't private" (self-signed certificate) before
the Welcome page.

Finding: once the last WSL process on Windows exits, WSL stops the Ubuntu
instance a few minutes later and the node goes down with it (Edge: connection
refused). Waking the distribution brings Docker and the node back by itself
(first 200 after about 25 s). A shipped Windows install needs something that
keeps the distribution running (a sign-in task or tray process).

Windows Server is not Windows 11. WSL2, Docker in WSL and the browser behave
the same for the installer; the consumer first-run layer does not. Confirm on
a real Windows 11 machine before calling the Windows path done.
