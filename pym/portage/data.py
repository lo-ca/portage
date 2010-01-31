# data.py -- Calculated/Discovered Data Values
# Copyright 1998-2009 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2
# $Id$

import os, sys, pwd, grp, platform
from portage.const import rootuid, portageuser, portagegroup, EPREFIX

import portage
portage.proxy.lazyimport.lazyimport(globals(),
	'portage.output:colorize',
	'portage.util:writemsg',
)
from portage.localization import _

ostype=platform.system()
userland = None
# Prefix always has USERLAND=GNU, even on
# FreeBSD, OpenBSD and Darwin (thank the lord!).
# Hopefully this entire USERLAND hack can go once
if EPREFIX == "" and (ostype == "DragonFly" or ostype.endswith("BSD")):
	userland = "BSD"
else:
	userland = "GNU"

lchown = getattr(os, "lchown", None)

if not lchown:
	if ostype == "Darwin":
		def lchown(*pos_args, **key_args):
			pass
	else:
		try:
			import missingos
			lchown = missingos.lchown
		except ImportError:
			def lchown(*pos_args, **key_args):
				writemsg(colorize("BAD", "!!!") + _(
					" It seems that os.lchown does not"
					" exist.  Please rebuild python.\n"), noiselevel=-1)
			lchown()

lchown = portage._unicode_func_wrapper(lchown)

def portage_group_warning():
	warn_prefix = colorize("BAD", "*** WARNING ***  ")
	mylines = [
		"For security reasons, only system administrators should be",
		"allowed in the portage group.  Untrusted users or processes",
		"can potentially exploit the portage group for attacks such as",
		"local privilege escalation."
	]
	for x in mylines:
		writemsg(warn_prefix, noiselevel=-1)
		writemsg(x, noiselevel=-1)
		writemsg("\n", noiselevel=-1)
	writemsg("\n", noiselevel=-1)

# Portage has 3 security levels that depend on the uid and gid of the main
# process and are assigned according to the following table:
#
# Privileges  secpass  uid    gid
# normal      0        any    any
# group       1        any    portage_gid
# super       2        0      any
#
# If the "wheel" group does not exist then wheelgid falls back to 0.
# If the "portage" group does not exist then portage_uid falls back to wheelgid.

secpass=0

uid=os.getuid()
wheelgid=0

if uid==rootuid:
	secpass=2
try:
	wheelgid=grp.getgrnam("wheel")[2]
except KeyError:
	pass

#Discover the uid and gid of the portage user/group
try:
	portage_uid=pwd.getpwnam(portageuser)[2]
	portage_gid=grp.getgrnam(portagegroup)[2]
	if secpass < 1 and portage_gid in os.getgroups():
		secpass=1
except KeyError:
	portage_uid=0
	portage_gid=0
	userpriv_groups = [portage_gid]
	writemsg(colorize("BAD",
		"portage: "+portageuser+" user or "+portagegroup+" group missing.") + "\n", noiselevel=-1)
	writemsg(colorize("BAD",
		"         In Prefix Portage this is quite dramatic") + "\n", noiselevel=-1)
	writemsg(colorize("BAD",
		"         since it means you have thrown away yourself.") + "\n", noiselevel=-1)
	writemsg(colorize("BAD",
		"         Re-add yourself or re-bootstrap Gentoo Prefix.") + "\n", noiselevel=-1)
# we need to fix this one day to distinguish prefix vs non-prefix
#		_("portage: 'portage' user or group missing.")) + "\n", noiselevel=-1)
#	writemsg(_(
#		"         For the defaults, line 1 goes into passwd, "
#		"and 2 into group.\n"), noiselevel=-1)
#	writemsg(colorize("GOOD",
#		"         portage:x:250:250:portage:/var/tmp/portage:/bin/false") \
#		+ "\n", noiselevel=-1)
#	writemsg(colorize("GOOD", "         portage::250:portage") + "\n",
#		noiselevel=-1)
	portage_group_warning()
else:
	userpriv_groups = [portage_gid]
	if secpass >= 2:
		# Get a list of group IDs for the portage user. Do not use
		# grp.getgrall() since it is known to trigger spurious
		# SIGPIPE problems with nss_ldap.
		try:
			from subprocess import getstatusoutput
		except ImportError:
			from commands import getstatusoutput
		mystatus, myoutput = getstatusoutput("id -G " + portageuser)
		if mystatus == os.EX_OK:
			for x in myoutput.split():
				try:
					userpriv_groups.append(int(x))
				except ValueError:
					pass
				del x
			userpriv_groups = list(set(userpriv_groups))
		del getstatusoutput, mystatus, myoutput
