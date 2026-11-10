import os
import shutil
import tempfile
import unittest

from clonix import password as pw
from clonix.errors import ClonixError

from tests.fakes import GIB, FakeRunner, lsblk_disk, lsblk_part

SHADOW = (
    "root:$6$old$AAAA:19000:0:99999:7:::\n"
    "admin:$6$sel$BBBB:19000:0:99999:7:::\n"
    "daemon:*:19000:0:99999:7:::\n"
)
PASSWD = (
    "root:x:0:0:root:/root:/bin/bash\n"
    "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
    "admin:x:1000:1000::/home/admin:/bin/bash\n"
    "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
)
CHNTPW_LIST = """chntpw version 1.00
| RID -|---------- Username ------------| Admin? |- Lock? --|
| 01f4 | Administrator                  | ADMIN  |          |
| 03e9 | Marie                          |        | dis/lock |
| 01f5 | Guest                          |        | *BLANK*  |
"""


def win_tree():
    return [lsblk_disk("sdw", 256 * GIB, [
        lsblk_part("sdw1", 100 * 1024 * 1024, "vfat", label="EFI"),
        lsblk_part("sdw2", 255 * GIB, "ntfs", label="Windows"),
    ])]


def linux_tree():
    return [lsblk_disk("sdx", 64 * GIB, [lsblk_part("sdx1", 64 * GIB, "ext4", label="root")])]


class PwRunner(FakeRunner):
    """FakeRunner qui simule le montage d'une partition sur un dossier fixture."""

    def __init__(self, tree, fixture, tools):
        super().__init__(tree=tree, tools=tools)
        self.fixture = fixture
        self.mountpoints = []

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.tools else None

    def run(self, argv, *, input=None, capture=True, readonly=False, check=True):
        argv = [str(a) for a in argv]
        if argv[0] == "mount":
            mp = argv[-1]
            self.mountpoints.append(mp)
            shutil.copytree(self.fixture, mp, dirs_exist_ok=True)
            return ""
        if argv[0] in ("umount", "sync"):
            return ""
        if argv[:2] == ["chntpw", "-l"]:
            self.calls.append(("run", argv, input))
            return CHNTPW_LIST
        if argv[0] == "chntpw" or argv[:2] == ["openssl", "passwd"]:
            self.calls.append(("run", argv, input))
            return "$6$abc$HASHED\n" if argv[0] == "openssl" else ""
        return super().run(argv, input=input, capture=capture, readonly=readonly, check=check)

    def written_shadow(self):
        with open(os.path.join(self.mountpoints[-1], "etc", "shadow")) as f:
            return f.read()


def make_linux_fixture():
    root = tempfile.mkdtemp(prefix="fx-lin-")
    os.makedirs(os.path.join(root, "etc"))
    with open(os.path.join(root, "etc", "shadow"), "w") as f:
        f.write(SHADOW)
    with open(os.path.join(root, "etc", "passwd"), "w") as f:
        f.write(PASSWD)
    return root


def make_windows_fixture():
    root = tempfile.mkdtemp(prefix="fx-win-")
    cfg = os.path.join(root, "Windows", "System32", "config")
    os.makedirs(cfg)
    with open(os.path.join(cfg, "SAM"), "wb") as f:
        f.write(b"regf-fake")
    return root


class PureTest(unittest.TestCase):
    def test_detect_windows(self):
        fx = make_windows_fixture()
        self.addCleanup(shutil.rmtree, fx)
        self.assertEqual(pw.detect_os(fx), pw.WINDOWS)
        self.assertTrue(pw.find_sam(fx).endswith("SAM"))
        # casse différente (ntfs-3g peut exposer "WINDOWS")
        os.rename(os.path.join(fx, "Windows"), os.path.join(fx, "WINDOWS"))
        self.assertEqual(pw.detect_os(fx), pw.WINDOWS)

    def test_detect_linux_and_none(self):
        fx = make_linux_fixture()
        self.addCleanup(shutil.rmtree, fx)
        self.assertEqual(pw.detect_os(fx), pw.LINUX)
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        self.assertIsNone(pw.detect_os(empty))

    def test_parse_passwd(self):
        accts = pw.parse_passwd(PASSWD)
        self.assertEqual(accts, [("root", 0, True), ("admin", 1000, False)])

    def test_parse_chntpw_users(self):
        self.assertEqual(pw.parse_chntpw_users(CHNTPW_LIST), ["Administrator", "Marie", "Guest"])

    def test_edit_shadow_clear_and_set(self):
        cleared = pw.edit_shadow(SHADOW, "root", "")
        self.assertIn("root::19000", cleared)
        self.assertIn("admin:$6$sel$BBBB", cleared)  # les autres comptes intacts
        changed = pw.edit_shadow(SHADOW, "admin", "$6$new$ZZZZ")
        self.assertIn("admin:$6$new$ZZZZ:19000", changed)
        with self.assertRaises(ClonixError):
            pw.edit_shadow(SHADOW, "inconnu", "")

    def test_chntpw_command(self):
        self.assertEqual(pw.chntpw_clear_cmd("/mnt/SAM", "Administrator"),
                         ["chntpw", "-u", "Administrator", "/mnt/SAM"])
        self.assertEqual(pw.CHNTPW_CLEAR_INPUT, "1\nq\ny\n")

    def test_hash_password(self):
        r = PwRunner(linux_tree(), make_linux_fixture(), ["openssl"])
        self.addCleanup(shutil.rmtree, r.fixture)
        digest = pw.hash_password(r, "secret")
        self.assertTrue(digest.startswith("$6$"))
        call = next(c for c in r.calls if c[1][0] == "openssl")
        self.assertEqual(call[1], ["openssl", "passwd", "-6", "-stdin"])
        self.assertEqual(call[2], "secret\n")  # mot de passe passé en entrée, pas en argument


class FlowTest(unittest.TestCase):
    def _linux(self):
        fx = make_linux_fixture()
        self.addCleanup(shutil.rmtree, fx)
        r = PwRunner(linux_tree(), fx, ["openssl"])
        return r

    def test_accounts_linux(self):
        info = pw.accounts(self._linux(), "/dev/sdx")
        self.assertEqual(info["os"], "linux")
        self.assertEqual([a["name"] for a in info["accounts"]], ["root", "admin"])
        self.assertTrue(info["accounts"][0]["admin"])

    def test_accounts_windows(self):
        fx = make_windows_fixture()
        self.addCleanup(shutil.rmtree, fx)
        r = PwRunner(win_tree(), fx, ["chntpw"])
        info = pw.accounts(r, "/dev/sdw")
        self.assertEqual(info["os"], "windows")
        self.assertEqual([a["name"] for a in info["accounts"]], ["Administrator", "Marie", "Guest"])
        self.assertTrue(info["accounts"][0]["admin"])

    def test_reset_linux_clear(self):
        r = self._linux()
        asked = []
        pw.reset(r, "/dev/sdx", "root", "clear", confirm=lambda *a: asked.append(a))
        self.assertTrue(asked)
        self.assertIn("root::19000", r.written_shadow())

    def test_reset_linux_set(self):
        r = self._linux()
        pw.reset(r, "/dev/sdx", "admin", "set", password="monMotDePasse")
        self.assertIn("admin:$6$abc$HASHED:19000", r.written_shadow())

    def test_reset_windows_clear(self):
        fx = make_windows_fixture()
        self.addCleanup(shutil.rmtree, fx)
        r = PwRunner(win_tree(), fx, ["chntpw"])
        pw.reset(r, "/dev/sdw", "Administrator", "clear")
        call = next(c for c in r.calls if c[1][0] == "chntpw" and "-u" in c[1])
        self.assertEqual(call[1], ["chntpw", "-u", "Administrator", pw.find_sam(r.mountpoints[-1])])
        self.assertEqual(call[2], pw.CHNTPW_CLEAR_INPUT)

    def test_reset_windows_set_refused(self):
        fx = make_windows_fixture()
        self.addCleanup(shutil.rmtree, fx)
        r = PwRunner(win_tree(), fx, ["chntpw"])
        with self.assertRaisesRegex(ClonixError, "Windows"):
            pw.reset(r, "/dev/sdw", "Administrator", "set", password="x")

    def test_reset_refuses_live_usb(self):
        tree = linux_tree() + [lsblk_disk("sdu", 16 * GIB,
                               [lsblk_part("sdu1", 16 * GIB, "iso9660", mount="/run/live/medium")],
                               tran="usb", rm=True)]
        r = PwRunner(tree, make_linux_fixture(), ["openssl"])
        self.addCleanup(shutil.rmtree, r.fixture)
        with self.assertRaises(ClonixError):
            pw.reset(r, "/dev/sdu", "root", "clear")

    def test_reset_no_system_partition(self):
        tree = [lsblk_disk("sdz", 8 * GIB, [lsblk_part("sdz1", 8 * GIB, "vfat")])]
        r = PwRunner(tree, make_linux_fixture(), ["openssl"])
        self.addCleanup(shutil.rmtree, r.fixture)
        with self.assertRaisesRegex(ClonixError, "aucune partition"):
            pw.reset(r, "/dev/sdz", "root", "clear")


if __name__ == "__main__":
    unittest.main()
