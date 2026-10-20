import os
import pwd
import shutil

BACKUP_USER_HOME = "/home/backupuser"
AUTHORIZED_KEYS_PATH = os.path.join(BACKUP_USER_HOME, ".ssh", "authorized_keys")
INCOMING_DIR = os.path.join(BACKUP_USER_HOME, "incoming")
WEB_BACKUPS_DIR = "/var/www/backups"
SESSION_LOG_DIR = "/home/backupuser/tmp-logs"
WRAPPER_PATH = os.path.join(BACKUP_USER_HOME, "ssh-wrapper.sh")

try:
    user_info = pwd.getpwnam("backupuser")
    BACKUP_UID = user_info.pw_uid
    BACKUP_GID = user_info.pw_gid
except KeyError:
    BACKUP_UID = 1001
    BACKUP_GID = 1001


# ponytail: atomic file replace — tempfile + os.rename, used by 4 call sites
import tempfile


def atomic_replace_lines(path: str, keep_fn):
    """Read lines from path, filter via keep_fn(line)->bool, write back atomically.
    No-op if path doesn't exist or no lines removed.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = f.readlines()
        keep = [l for l in old if keep_fn(l)]
        if len(keep) == len(old):
            return
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.chmod(tmp, 0o600)
            os.chown(tmp, BACKUP_UID, BACKUP_GID)
            os.rename(tmp, path)
        except OSError:
            os.unlink(tmp)
    except OSError:
        pass


def ensure_ssh_wrapper():
    """Install the reviewed, root-owned forced-command wrapper."""
    shutil.copyfile("/opt/backup-api/ssh-wrapper.sh", WRAPPER_PATH)
    os.chmod(WRAPPER_PATH, 0o755)
    os.chown(WRAPPER_PATH, BACKUP_UID, BACKUP_GID)
