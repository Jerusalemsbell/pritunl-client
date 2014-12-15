import optparse
import datetime
import re
import sys
import json
import os
import subprocess
import time
import getpass
import zlib
import math
import pymongo
import requests
import werkzeug.http

UBUNTU_RELEASES = [
    'trusty', # 14.04
    'precise', # 12.04
    'utopic', # 14.10
]

USAGE = """Usage: builder [command] [options]
Command Help: builder [command] --help

Commands:
  version               Print the version and exit
  sync-db               Sync database
  set-version           Set current version
  build                 Build and release"""

INIT_PATH = 'pritunl_client/__init__.py'
SETUP_PATH = 'setup.py'
CHANGES_PATH = 'CHANGES'
PPA_NAME = 'pritunl'
DEBIAN_CHANGELOG_PATH = 'debian/changelog'
DEBIAN_GTK_CHANGELOG_PATH = 'debian-gtk/changelog'
BUILD_KEYS_PATH = 'tools/build_keys.json'
ARCH_PKGBUILD_PATH = 'arch/production/PKGBUILD'
ARCH_DEV_PKGBUILD_PATH = 'arch/dev/PKGBUILD'
ARCH_PKGINSTALL = 'arch/production/pritunl-client.install'
ARCH_DEV_PKGINSTALL = 'arch/dev/pritunl-client.install'
ARCH_PKGBUILD_GTK_PATH = 'arch/production-gtk/PKGBUILD'
ARCH_DEV_PKGBUILD_GTK_PATH = 'arch/dev-gtk/PKGBUILD'
ARCH_PKGINSTALL_GTK = 'arch/production-gtk/pritunl-client-gtk.install'
ARCH_DEV_PKGINSTALL_GTK = 'arch/dev-gtk/pritunl-client-gtk.install'
PRIVATE_KEY_NAME = 'private_key.asc'
AUR_CATEGORY = 13

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
cur_date = datetime.datetime.utcnow()

def vagrant_popen(cmd, cwd=None, name=''):
    if cwd:
        cmd = 'cd /vagrant/%s; %s' % (cwd, cmd)
    return subprocess.Popen("vagrant ssh --command='%s' %s" % (cmd, name),
        shell=True, stdin=subprocess.PIPE)

def vagrant_check_call(cmd, cwd=None, name=''):
    if cwd:
        cmd = 'cd /vagrant/%s; %s' % (cwd, cmd)
    return subprocess.check_call("vagrant ssh --command='%s' %s" % (cmd, name),
        shell=True, stdin=subprocess.PIPE)

def wget(url, cwd=None, output=None):
    if output:
        args = ['wget', '-O', output, url]
    else:
        args = ['wget', url]
    subprocess.check_call(args, cwd=cwd)

def post_git_asset(release_id, file_name, file_path):
    file_size = os.path.getsize(file_path)
    response = requests.post(
        'https://uploads.github.com/repos/%s/%s/releases/%s/assets' % (
            github_owner, pkg_name, release_id),
        verify=False,
        headers={
            'Authorization': 'token %s' % github_token,
            'Content-Type': 'application/octet-stream',
            'Content-Size': file_size,
        },
        params={
            'name': file_name,
        },
        data=open(file_path, 'rb').read(),
    )

    if response.status_code != 201:
        print 'Failed to create release on github'
        print response.json()
        sys.exit(1)

def rm_tree(path):
    subprocess.check_call(['rm', '-rf', path])

def tar_extract(archive_path, cwd=None):
    subprocess.check_call(['tar', 'xfz', archive_path], cwd=cwd)

def tar_compress(archive_path, in_path, cwd=None):
    subprocess.check_call(['tar', 'cfz', archive_path, in_path], cwd=cwd)

def get_ver(version):
    day_num = (cur_date - datetime.datetime(2013, 9, 12)).days
    min_num = int(math.floor(((cur_date.hour * 60) + cur_date.minute) / 14.4))
    ver = re.findall(r'\d+', version)
    ver_str = '.'.join((ver[0], ver[1], str(day_num), str(min_num)))

    name = ''.join(re.findall('[a-z]+', version))
    if name:
        ver_str += name + ver[2]

    return ver_str

def get_int_ver(version):
    ver = re.findall(r'\d+', version)

    if 'snapshot' in version:
        pass
    elif 'alpha' in version:
        ver[-1] = str(int(ver[-1]) + 1000)
    elif 'beta' in version:
        ver[-1] = str(int(ver[-1]) + 2000)
    elif 'rc' in version:
        ver[-1] = str(int(ver[-1]) + 3000)
    else:
        ver[-1] = str(int(ver[-1]) + 4000)

    return int(''.join([x.zfill(4) for x in ver]))

def generate_last_modifited_etag(file_path):
    file_name = os.path.basename(file_path).encode(sys.getfilesystemencoding())
    file_mtime = datetime.datetime.utcfromtimestamp(
        os.path.getmtime(file_path))
    file_size = int(os.path.getsize(file_path))
    last_modified = werkzeug.http.http_date(file_mtime)

    return (last_modified, 'wzsdm-%d-%s-%s' % (
        time.mktime(file_mtime.timetuple()),
        file_size,
        zlib.adler32(file_name) & 0xffffffff,
    ))


# Load build keys
with open(BUILD_KEYS_PATH, 'r') as build_keys_file:
    build_keys = json.loads(build_keys_file.read().strip())
    github_owner = build_keys['github_owner']
    github_token = build_keys['github_token']
    aur_username = build_keys['aur_username']
    aur_password = build_keys['aur_password']
    private_key = build_keys['private_key']

# Get package info
with open(INIT_PATH, 'r') as init_file:
    for line in init_file.readlines():
        line = line.strip()

        if line[:9] == '__title__':
            app_name = line.split('=')[1].replace("'", '').strip()
            pkg_name = app_name.replace('_', '-')

        elif line[:10] == '__author__':
            maintainer = line.split('=')[1].replace("'", '').strip()

        elif line[:9] == '__email__':
            maintainer_email = line.split('=')[1].replace("'", '').strip()

        elif line[:11] == '__version__':
            key, val = line.split('=')
            cur_version = line.split('=')[1].replace("'", '').strip()

# Parse args
if len(sys.argv) > 1:
    cmd = sys.argv[1]
else:
    cmd = 'version'

parser = optparse.OptionParser(usage=USAGE)

parser.add_option('--test', action='store_true',
    help='Upload to test repo')

(options, args) = parser.parse_args()

build_num = 0

if cmd == 'version':
    print '%s v%s' % (app_name, cur_version)
    sys.exit(0)


elif cmd == 'set-version':
    new_version_orig = args[1]
    new_version = get_ver(new_version_orig)

    is_snapshot = 'snapshot' in new_version
    is_dev_release = any((
        'snapshot' in new_version,
        'alpha' in new_version,
        'beta' in new_version,
        'rc' in new_version,
    ))


    # Update changes
    with open(CHANGES_PATH, 'r') as changes_file:
        changes_data = changes_file.read()

    with open(CHANGES_PATH, 'w') as changes_file:
        ver_date_str = 'Version ' + new_version.replace(
            'v', '') + cur_date.strftime(' %Y-%m-%d')
        changes_file.write(changes_data.replace(
            '<%= version %>',
            '%s\n%s' % (ver_date_str, '-' * len(ver_date_str)),
        ))


    # Check for duplicate version
    response = requests.get(
        'https://api.github.com/repos/%s/%s/releases' % (
            github_owner, pkg_name),
        headers={
            'Authorization': 'token %s' % github_token,
            'Content-type': 'application/json',
        },
    )

    if response.status_code != 200:
        print 'Failed to get repo releases on github'
        print response.json()
        sys.exit(1)

    for release in response.json():
        if release['tag_name'] == new_version:
            print 'Version already exists in github'
            sys.exit(1)


    # Generate changelog
    debian_changelog = ''
    changelog_version = None
    release_body = ''
    snapshot_lines = []
    if is_snapshot:
        snapshot_lines.append('Version %s %s' % (
            new_version, datetime.datetime.utcnow().strftime('%Y-%m-%d')))
        snapshot_lines.append('Snapshot release')

    with open(CHANGES_PATH, 'r') as changelog_file:
        for line in snapshot_lines + changelog_file.readlines()[2:]:
            line = line.strip()

            if not line or line[0] == '-':
                continue

            if line[:7] == 'Version':
                if debian_changelog:
                    debian_changelog += '\n -- %s <%s>  %s -0400\n\n' % (
                        maintainer,
                        maintainer_email,
                        date.strftime('%a, %d %b %Y %H:%M:%S'),
                    )

                _, version, date = line.split(' ')
                date = datetime.datetime.strptime(date, '%Y-%m-%d')

                if not changelog_version:
                    changelog_version = version

                debian_changelog += \
                    '%s (%s-%subuntu1) unstable; urgency=low\n\n' % (
                    pkg_name, version, build_num)

            elif debian_changelog:
                debian_changelog += '  * %s\n' % line

                if not is_snapshot and version == new_version:
                    release_body += '* %s\n' % line

        debian_changelog += '\n -- %s <%s>  %s -0400\n' % (
            maintainer,
            maintainer_email,
            date.strftime('%a, %d %b %Y %H:%M:%S'),
        )

    if not is_snapshot and changelog_version != new_version:
        print 'New version does not exist in changes'
        sys.exit(1)

    with open(DEBIAN_CHANGELOG_PATH, 'w') as changelog_file:
        changelog_file.write(debian_changelog)

    with open(DEBIAN_GTK_CHANGELOG_PATH, 'w') as changelog_file:
        changelog_file.write(debian_changelog.replace(
            pkg_name, pkg_name + '-gtk'))

    if not is_snapshot and not release_body:
        print 'Failed to generate github release body'
        sys.exit(1)
    elif is_snapshot:
        release_body = '* Snapshot release'
    release_body = release_body.rstrip('\n')


    # Update arch package
    for pkgbuild_path in (
            ARCH_DEV_PKGBUILD_PATH if is_dev_release else ARCH_PKGBUILD_PATH,
            ARCH_DEV_PKGBUILD_GTK_PATH if is_dev_release else \
                ARCH_PKGBUILD_GTK_PATH,
            ):
        with open(pkgbuild_path, 'r') as pkgbuild_file:
            pkgbuild_data = re.sub(
                'pkgver=(.*)',
                'pkgver=%s' % new_version,
                pkgbuild_file.read(),
            )
            pkgbuild_data = re.sub(
                'pkgrel=(.*)',
                'pkgrel=%s' % (build_num + 1),
                pkgbuild_data,
            )

        with open(pkgbuild_path, 'w') as pkgbuild_file:
            pkgbuild_file.write(pkgbuild_data)


    # Update init
    with open(INIT_PATH, 'r') as init_file:
        init_data = init_file.read()

    with open(INIT_PATH, 'w') as init_file:
        init_file.write(re.sub(
            "(__version__ = )('.*?')",
            "__version__ = '%s'" % new_version,
            init_data,
        ))


    # Update setup
    with open(SETUP_PATH, 'r') as setup_file:
        setup_data = setup_file.read()

    with open(SETUP_PATH, 'w') as setup_file:
        setup_file.write(re.sub(
            "(VERSION = )('.*?')",
            "VERSION = '%s'" % new_version,
            setup_data,
        ))


    # Git commit
    subprocess.check_call(['git', 'reset', 'HEAD', '.'])
    subprocess.check_call(['git', 'add', CHANGES_PATH])
    subprocess.check_call(['git', 'add', INIT_PATH])
    subprocess.check_call(['git', 'add', SETUP_PATH])
    subprocess.check_call(['git', 'add', DEBIAN_CHANGELOG_PATH])
    subprocess.check_call(['git', 'add', DEBIAN_GTK_CHANGELOG_PATH])
    subprocess.check_call(['git', 'add', ARCH_PKGBUILD_PATH])
    subprocess.check_call(['git', 'add', ARCH_DEV_PKGBUILD_PATH])
    subprocess.check_call(['git', 'add', ARCH_PKGBUILD_GTK_PATH])
    subprocess.check_call(['git', 'add', ARCH_DEV_PKGBUILD_GTK_PATH])
    subprocess.check_call(['git', 'commit', '-m', 'Create new release'])
    subprocess.check_call(['git', 'push'])


    # Create branch
    if not is_dev_release:
        subprocess.check_call(['git', 'branch', new_version])
        subprocess.check_call(['git', 'push', '-u', 'origin', new_version])
    time.sleep(8)


    # Create release
    response = requests.post(
        'https://api.github.com/repos/%s/%s/releases' % (
            github_owner, pkg_name),
        headers={
            'Authorization': 'token %s' % github_token,
            'Content-type': 'application/json',
        },
        data=json.dumps({
            'tag_name': new_version,
            'name': '%s v%s' % (pkg_name, new_version),
            'body': release_body,
            'prerelease': is_dev_release,
            'target_commitish': 'master' if is_dev_release else new_version,
        }),
    )

    if response.status_code != 201:
        print 'Failed to create release on github'
        print response.json()
        sys.exit(1)


elif cmd == 'build':
    is_dev_release = any((
        'snapshot' in cur_version,
        'alpha' in cur_version,
        'beta' in cur_version,
        'rc' in cur_version,
    ))

    # Get password
    passphrase = getpass.getpass('Enter GPG passphrase: ')
    passphrase_retype = getpass.getpass('Retype GPG passphrase: ')

    if passphrase != passphrase_retype:
        print 'Passwords do not match'
        sys.exit(1)


    # Start vagrant boxes
    subprocess.check_call(['vagrant', 'up'])


    # Create debian package
    for build_dir in (
                'build/%s/debian' % cur_version,
                'build/%s/debian-gtk' % cur_version,
            ):
        if not os.path.isdir(build_dir):
            os.makedirs(build_dir)


        # Import gpg key
        private_key_path = os.path.join(build_dir, PRIVATE_KEY_NAME)
        with open(private_key_path, 'w') as private_key_file:
            private_key_file.write(private_key)

        vagrant_check_call('sudo gpg --import private_key.asc || true',
            cwd=build_dir)

        os.remove(private_key_path)


        # Download archive
        archive_name = '%s.tar.gz' % cur_version
        archive_path = os.path.join(build_dir, archive_name)
        if not os.path.isfile(archive_path):
            wget('https://github.com/%s/%s/archive/%s' % (
                    github_owner, pkg_name, archive_name),
                output=archive_name,
                cwd=build_dir,
            )


        # Create orig archive
        orig_name = '%s_%s.orig.tar.gz' % (pkg_name, cur_version)
        orig_path = os.path.join(build_dir, orig_name)
        if not os.path.isfile(orig_path):
            subprocess.check_call(['cp', archive_name, orig_name],
                cwd=build_dir)


        # Create build path
        build_name = '%s-%s' % (pkg_name, cur_version)
        build_path = os.path.join(build_dir, build_name)
        if not os.path.isdir(build_path):
            tar_extract(archive_name, cwd=build_dir)


        # Setup gtk build
        if 'gtk' in build_dir:
            subprocess.check_call(['rm', '-rf', 'debian'], cwd=build_path)
            subprocess.check_call(['mv', 'debian-gtk', 'debian'],
                cwd=build_path)


        # Read changelog
        changelog_path = os.path.join(build_path, DEBIAN_CHANGELOG_PATH)
        with open(changelog_path, 'r') as changelog_file:
            changelog_data = changelog_file.read()


        # Build debian packages
        for ubuntu_release in UBUNTU_RELEASES:
            with open(changelog_path, 'w') as changelog_file:
                changelog_file.write(re.sub(
                    'ubuntu1(.*);',
                    'ubuntu1~%s) %s;' % (ubuntu_release, ubuntu_release),
                    changelog_data,
                ))

            vagrant_check_call(
                'sudo debuild -p"gpg --no-tty --passphrase %s"' % (
                    passphrase),
                cwd=build_path,
            )

            if 'gtk' in build_dir:
                vagrant_check_call(
                    'sudo debuild -S -sd -p"gpg --no-tty --passphrase %s"' % (
                        passphrase),
                    cwd=build_path,
                )
            else:
                vagrant_check_call(
                    'sudo debuild -S -p"gpg --no-tty --passphrase %s"' % (
                        passphrase),
                    cwd=build_path,
                )


    # Create arch package
    for build_dir, pkgbuild_path, pkginstall_path in (
                (
                    'build/%s/arch' % cur_version,
                    ARCH_DEV_PKGBUILD_PATH if is_dev_release else \
                        ARCH_PKGBUILD_PATH,
                    ARCH_DEV_PKGINSTALL if is_dev_release else \
                        ARCH_PKGINSTALL,
                ),
                (
                    'build/%s/arch-gtk' % cur_version,
                    ARCH_DEV_PKGBUILD_GTK_PATH if is_dev_release else \
                        ARCH_PKGBUILD_GTK_PATH,
                    ARCH_DEV_PKGINSTALL_GTK if is_dev_release else \
                        ARCH_PKGINSTALL_GTK,
                ),
            ):
        if not os.path.isdir(build_dir):
            os.makedirs(build_dir)

        # Download archive
        archive_name = '%s.tar.gz' % cur_version
        archive_path = os.path.join(build_dir, archive_name)
        if not os.path.isfile(archive_path):
            wget('https://github.com/%s/%s/archive/%s' % (
                    github_owner, pkg_name, archive_name),
                output=archive_name,
                cwd=build_dir,
            )


        # Get sha256 sum
        archive_sha256_sum = subprocess.check_output(
            ['sha256sum', archive_path]).split()[0]


        # Generate pkgbuild
        with open(pkgbuild_path, 'r') as pkgbuild_file:
            pkgbuild_data = pkgbuild_file.read()
        pkgbuild_data = pkgbuild_data.replace('CHANGE_ME', archive_sha256_sum)
        pkgbuild_data = pkgbuild_data.replace('0.10.1', '1.0.459.9snapshot1')

        pkgbuild_path = os.path.join(build_dir, 'PKGBUILD')
        with open(pkgbuild_path, 'w') as pkgbuild_file:
             pkgbuild_file.write(pkgbuild_data)

        subprocess.check_call(['cp', pkginstall_path, build_dir])


        # Build arch package
        subprocess.check_call(['makepkg', '-f'], cwd=build_dir)
        subprocess.check_call(['mkaurball', '-f'], cwd=build_dir)


elif cmd == 'upload':
    is_dev_release = any((
        'snapshot' in cur_version,
        'alpha' in cur_version,
        'beta' in cur_version,
        'rc' in cur_version,
    ))


    # Get release id
    release_id = None
    response = requests.get(
        'https://api.github.com/repos/%s/%s/releases' % (
            github_owner, pkg_name),
        headers={
            'Authorization': 'token %s' % github_token,
            'Content-type': 'application/json',
        },
    )

    for release in response.json():
        if release['tag_name'] == cur_version:
            release_id = release['id']

    if not release_id:
        print 'Version does not exists in github'
        sys.exit(1)


    # Upload debian package
    build_dir = 'build/%s/debian' % cur_version

    if options.test:
        launchpad_ppa = '%s/%s-test' % (pkg_name, pkg_name)
    elif is_dev_release:
        launchpad_ppa = '%s/%s-dev' % (pkg_name, pkg_name)
    else:
        launchpad_ppa = '%s/ppa' % pkg_name

    for ubuntu_release in UBUNTU_RELEASES:
        deb_file_name = '%s_%s-%subuntu1~%s_all.deb' % (
            pkg_name,
            cur_version,
            build_num,
            ubuntu_release,
        )
        deb_file_path = os.path.join(build_dir, deb_file_name)
        post_git_asset(release_id, deb_file_name, deb_file_path)

        vagrant_check_call(
            'sudo dput -f ppa:%s %s_%s-%subuntu1~%s_source.changes' % (
                launchpad_ppa,
                pkg_name,
                cur_version,
                build_num,
                ubuntu_release,
            ),
            cwd=build_dir,
        )

    if options.test:
        sys.exit(0)


    # Upload arch package
    build_dir = 'build/%s/arch' % cur_version
    aur_pkg_name = '%s-%s-%s-any.pkg.tar.xz' % (
        pkg_name + '-dev' if is_dev_release else pkg_name,
        cur_version,
        build_num + 1,
    )
    aur_path = os.path.join(build_dir, aur_pkg_name)
    aurball_pkg_name = '%s-%s-%s.src.tar.gz' % (
        pkg_name + '-dev' if is_dev_release else pkg_name,
        cur_version,
        build_num + 1,
    )
    aurball_path = os.path.join(build_dir, aurball_pkg_name)

    post_git_asset(release_id, aur_pkg_name, aur_path)

    session = requests.Session()

    response = session.post('https://aur.archlinux.org/login',
        data={
            'user': aur_username,
            'passwd': aur_password,
            'remember_me': 'on',
        },
    )

    response = session.get('https://aur.archlinux.org/submit/')
    token = re.findall(
        '(name="token" value=)("?.*")',
        response.text,
    )[0][1].replace('"', '')

    response = session.post('https://aur.archlinux.org/submit/',
        files={
            'pfile': open(aurball_path, 'rb'),
        },
        data={
            'pkgsubmit': 1,
            'token': token,
            'category': AUR_CATEGORY,
        }
    )


    # Upload centos package
    rpms_dir = 'build/%s/centos/RPMS/x86_64' % cur_version
    rpm_name = '%s-%s-%s.el7.centos.x86_64.rpm' % (
        pkg_name + '-dev' if is_dev_release else pkg_name,
        cur_version,
        build_num + 1,
    )
    rpm_path = os.path.join(rpms_dir, rpm_name)

    post_git_asset(release_id, rpm_name, rpm_path)


else:
    print 'Unknown command'
    sys.exit(1)
