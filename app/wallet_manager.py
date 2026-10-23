"""Public interface contains no plaintext Keypair; signing runs in a child."""
import json,os,stat,subprocess
from pathlib import Path

class PermissionError(RuntimeError):pass

def windows_acl(path,restrict=False):
    # A constant script and a literal path passed separately through environment;
    # no private key/password ever enters PowerShell, argv or its output.
    script=r'''
$ErrorActionPreference='Stop'
$p=$env:PUMP_BOT_ACL_TARGET
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
if ($env:PUMP_BOT_ACL_RESTRICT -eq '1') {
  if ((Get-Item -LiteralPath $p).PSIsContainer) {$acl=New-Object System.Security.AccessControl.DirectorySecurity}
  else {$acl=New-Object System.Security.AccessControl.FileSecurity}
  $acl.SetOwner($sid)
  $acl.SetAccessRuleProtection($true,$false)
  foreach ($s in @($sid.Value,'S-1-5-18','S-1-5-32-544')) {
    $identity=New-Object System.Security.Principal.SecurityIdentifier($s)
    $rule=New-Object System.Security.AccessControl.FileSystemAccessRule($identity,'FullControl','Allow')
    $acl.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $p -AclObject $acl
}
$acl=Get-Acl -LiteralPath $p
if (!$acl.AreAccessRulesProtected) {throw 'INHERITED_ACL'}
$owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
if ($owner -ne $sid.Value) {throw 'UNEXPECTED_OWNER'}
$rules=$acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])
foreach ($r in $rules) {
  if ($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Value -notin @($sid.Value,'S-1-5-18','S-1-5-32-544')) {throw 'BROAD_ALLOW_ACL'}
}
Write-Output 'PRIVATE_ACL_OK'
'''
    env=dict(os.environ,PUMP_BOT_ACL_TARGET=str(Path(path).resolve()),PUMP_BOT_ACL_RESTRICT='1' if restrict else '0')
    try:
        p=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,text=True,timeout=15)
        if p.returncode!=0 or 'PRIVATE_ACL_OK' not in p.stdout:raise ValueError()
    except Exception:raise PermissionError('WINDOWS_PRIVATE_ACL_NOT_VERIFIED') from None

def assert_private(path):
    p=Path(path);info=p.lstat()
    if stat.S_ISLNK(info.st_mode):raise PermissionError('SYMLINK_WALLET_NOT_ALLOWED')
    if os.name=='nt':windows_acl(p)
    else:
        if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:raise PermissionError('WALLET_PERMISSIONS_TOO_OPEN')

def restrict_access(path):
    if os.name=='nt':windows_acl(path,restrict=True)
    else:os.chmod(path,0o700 if Path(path).is_dir() else 0o600)
    assert_private(path)

class SecureWallet:
    def __init__(self,vault=None,policy=None):
        import wallet_signer
        self.vault=Path(vault or wallet_signer.PRIVATE/'wallet.enc.json')
        self.policy=Path(policy or wallet_signer.PRIVATE/'signer_policy.json')
        assert_private(self.vault);assert_private(self.policy)
        self._public=json.loads(self.vault.read_text())['public_key']
    def pubkey(self):return self._public
    def self_test(self,password):
        import wallet_signer
        return wallet_signer.self_test_isolated(self.vault,password)
    def sign(self,tx_bytes,password):
        import base64,wallet_signer
        from solders.transaction import VersionedTransaction
        from solders.message import to_bytes_versioned
        tx=VersionedTransaction.from_bytes(tx_bytes)
        assert_private(self.vault);assert_private(self.policy)
        encoded=base64.b64encode(to_bytes_versioned(tx.message)).decode()
        return base64.b64decode(wallet_signer.sign_isolated(encoded,self.vault,self.policy,password),validate=True)
