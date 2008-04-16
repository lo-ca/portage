# Copyright 1998-2007 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2
# $Id$

from portage.cache.cache_errors import CacheError
from portage.const import REPO_NAME_LOC
from portage.data import portage_gid, secpass
from portage.dbapi import dbapi
from portage.dep import use_reduce, paren_reduce, dep_getslot, dep_getkey, \
	match_from_list, match_to_list, remove_slot
from portage.exception import OperationNotPermitted, PortageException, \
	UntrustedSignature, SecurityViolation, InvalidSignature, MissingSignature, \
	FileNotFound, InvalidDependString
from portage.manifest import Manifest
from portage.output import red
from portage.util import ensure_dirs, writemsg, apply_recursive_permissions
from portage.versions import pkgcmp, pkgsplit, catpkgsplit, best

import portage.gpg, portage.checksum

from portage import eclass_cache, auxdbkeys, auxdbkeylen, doebuild, flatten, \
	listdir, dep_expand, eapi_is_supported, key_expand, dep_check

import os, re, stat, sys
from itertools import izip

class portdbapi(dbapi):
	"""this tree will scan a portage directory located at root (passed to init)"""
	portdbapi_instances = []
	def __init__(self, porttree_root, mysettings=None):
		portdbapi.portdbapi_instances.append(self)

		from portage import config
		if mysettings:
			self.mysettings = mysettings
		else:
			from portage import settings
			self.mysettings = config(clone=settings)
		self._categories = set(self.mysettings.categories)
		# This is strictly for use in aux_get() doebuild calls when metadata
		# is generated by the depend phase.  It's safest to use a clone for
		# this purpose because doebuild makes many changes to the config
		# instance that is passed in.
		self.doebuild_settings = config(clone=self.mysettings)

		self.manifestVerifyLevel = None
		self.manifestVerifier = None
		self.manifestCache = {}    # {location: [stat, md5]}
		self.manifestMissingCache = []

		if "gpg" in self.mysettings.features:
			self.manifestVerifyLevel = portage.gpg.EXISTS
			if "strict" in self.mysettings.features:
				self.manifestVerifyLevel = portage.gpg.MARGINAL
				self.manifestVerifier = portage.gpg.FileChecker(self.mysettings["PORTAGE_GPG_DIR"], "gentoo.gpg", minimumTrust=self.manifestVerifyLevel)
			elif "severe" in self.mysettings.features:
				self.manifestVerifyLevel = portage.gpg.TRUSTED
				self.manifestVerifier = portage.gpg.FileChecker(self.mysettings["PORTAGE_GPG_DIR"], "gentoo.gpg", requireSignedRing=True, minimumTrust=self.manifestVerifyLevel)
			else:
				self.manifestVerifier = portage.gpg.FileChecker(self.mysettings["PORTAGE_GPG_DIR"], "gentoo.gpg", minimumTrust=self.manifestVerifyLevel)

		#self.root=settings["PORTDIR"]
		self.porttree_root = os.path.realpath(porttree_root)

		self.depcachedir = self.mysettings.depcachedir[:]

		self.eclassdb = eclass_cache.cache(self.porttree_root,
			overlays=self.mysettings["PORTDIR_OVERLAY"].split())

		# This is used as sanity check for aux_get(). If there is no
		# root eclass dir, we assume that PORTDIR is invalid or
		# missing. This check allows aux_get() to detect a missing
		# portage tree and return early by raising a KeyError.
		self._have_root_eclass_dir = os.path.isdir(
			os.path.join(self.porttree_root, "eclass"))

		self.metadbmodule = self.mysettings.load_best_module("portdbapi.metadbmodule")

		#if the portdbapi is "frozen", then we assume that we can cache everything (that no updates to it are happening)
		self.xcache = {}
		self.frozen = 0

		self.porttrees = [self.porttree_root] + \
			[os.path.realpath(t) for t in self.mysettings["PORTDIR_OVERLAY"].split()]
		self.treemap = {}
		self._repository_map = {}
		for path in self.porttrees:
			repo_name_path = os.path.join(path, REPO_NAME_LOC)
			try:
				repo_name = open(repo_name_path, 'r').readline().strip()
				self.treemap[repo_name] = path
				self._repository_map[path] = repo_name
			except (OSError,IOError):
				# warn about missing repo_name at some other time, since we
				# don't want to see a warning every time the portage module is
				# imported.
				pass
		
		self.auxdbmodule = self.mysettings.load_best_module("portdbapi.auxdbmodule")
		self.auxdb = {}
		self._init_cache_dirs()
		# XXX: REMOVE THIS ONCE UNUSED_0 IS YANKED FROM auxdbkeys
		# ~harring
		filtered_auxdbkeys = filter(lambda x: not x.startswith("UNUSED_0"), auxdbkeys)
		if secpass < 1:
			from portage.cache import metadata_overlay, volatile
			for x in self.porttrees:
				db_ro = self.auxdbmodule(self.depcachedir, x,
					filtered_auxdbkeys, gid=portage_gid, readonly=True)
				self.auxdb[x] = metadata_overlay.database(
					self.depcachedir, x, filtered_auxdbkeys,
					gid=portage_gid, db_rw=volatile.database,
					db_ro=db_ro)
		else:
			for x in self.porttrees:
				# location, label, auxdbkeys
				self.auxdb[x] = self.auxdbmodule(
					self.depcachedir, x, filtered_auxdbkeys, gid=portage_gid)
		# Selectively cache metadata in order to optimize dep matching.
		self._aux_cache_keys = set(
			["DEPEND", "EAPI", "IUSE", "KEYWORDS", "LICENSE",
			"PDEPEND", "PROVIDE", "RDEPEND", "repository",
			"RESTRICT", "SLOT"])
		self._aux_cache = {}
		self._broken_ebuilds = set()

	def _init_cache_dirs(self):
		"""Create /var/cache/edb/dep and adjust permissions for the portage
		group."""

		dirmode = 02070
		filemode =   060
		modemask =    02

		try:
			ensure_dirs(self.depcachedir, gid=portage_gid,
				mode=dirmode, mask=modemask)
		except PortageException, e:
			pass

	def close_caches(self):
		if not hasattr(self, "auxdb"):
			# unhandled exception thrown from constructor
			return
		for x in self.auxdb:
			self.auxdb[x].sync()
		self.auxdb.clear()

	def flush_cache(self):
		for x in self.auxdb.values():
			x.sync()

	def findLicensePath(self, license_name):
		mytrees = self.porttrees[:]
		mytrees.reverse()
		for x in mytrees:
			license_path = os.path.join(x, "licenses", license_name)
			if os.access(license_path, os.R_OK):
				return license_path
		return None

	def findname(self,mycpv):
		return self.findname2(mycpv)[0]

	def getRepositoryPath(self, repository_id):
		"""
		This function is required for GLEP 42 compliance; given a valid repository ID
		it must return a path to the repository
		TreeMap = { id:path }
		"""
		if repository_id in self.treemap:
			return self.treemap[repository_id]
		return None

	def getRepositories(self):
		"""
		This function is required for GLEP 42 compliance; it will return a list of
		repository ID's
		TreeMap = {id: path}
		"""
		return [k for k in self.treemap if k]

	def findname2(self, mycpv, mytree=None):
		""" 
		Returns the location of the CPV, and what overlay it was in.
		Searches overlays first, then PORTDIR; this allows us to return the first
		matching file.  As opposed to starting in portdir and then doing overlays
		second, we would have to exhaustively search the overlays until we found
		the file we wanted.
		"""
		if not mycpv:
			return "",0
		mysplit = mycpv.split("/")
		psplit = pkgsplit(mysplit[1])

		if mytree:
			mytrees = [mytree]
		else:
			mytrees = self.porttrees[:]
			mytrees.reverse()
		if psplit:
			for x in mytrees:
				file=x+"/"+mysplit[0]+"/"+psplit[0]+"/"+mysplit[1]+".ebuild"
				if os.access(file, os.R_OK):
					return[file, x]
		return None, 0

	def aux_get(self, mycpv, mylist, mytree=None):
		"stub code for returning auxilliary db information, such as SLOT, DEPEND, etc."
		'input: "sys-apps/foo-1.0",["SLOT","DEPEND","HOMEPAGE"]'
		'return: ["0",">=sys-libs/bar-1.0","http://www.foo.com"] or raise KeyError if error'
		cache_me = False
		if not mytree:
			cache_me = True
		if not mytree and not self._known_keys.intersection(
			mylist).difference(self._aux_cache_keys):
			aux_cache = self._aux_cache.get(mycpv)
			if aux_cache is not None:
				return [aux_cache.get(x, "") for x in mylist]
			cache_me = True
		global auxdbkeys, auxdbkeylen
		cat,pkg = mycpv.split("/", 1)

		myebuild, mylocation = self.findname2(mycpv, mytree)

		if not myebuild:
			writemsg("!!! aux_get(): ebuild path for '%(cpv)s' not specified:\n" % {"cpv":mycpv},
				noiselevel=1)
			writemsg("!!!            %s\n" % myebuild, noiselevel=1)
			raise KeyError(mycpv)

		myManifestPath = "/".join(myebuild.split("/")[:-1])+"/Manifest"
		if "gpg" in self.mysettings.features:
			try:
				mys = portage.gpg.fileStats(myManifestPath)
				if (myManifestPath in self.manifestCache) and \
				   (self.manifestCache[myManifestPath] == mys):
					pass
				elif self.manifestVerifier:
					if not self.manifestVerifier.verify(myManifestPath):
						# Verification failed the desired level.
						raise UntrustedSignature, "Untrusted Manifest: %(manifest)s" % {"manifest":myManifestPath}

				if ("severe" in self.mysettings.features) and \
				   (mys != portage.gpg.fileStats(myManifestPath)):
					raise SecurityViolation, "Manifest changed: %(manifest)s" % {"manifest":myManifestPath}

			except InvalidSignature, e:
				if ("strict" in self.mysettings.features) or \
				   ("severe" in self.mysettings.features):
					raise
				writemsg("!!! INVALID MANIFEST SIGNATURE DETECTED: %(manifest)s\n" % {"manifest":myManifestPath})
			except MissingSignature, e:
				if ("severe" in self.mysettings.features):
					raise
				if ("strict" in self.mysettings.features):
					if myManifestPath not in self.manifestMissingCache:
						writemsg("!!! WARNING: Missing signature in: %(manifest)s\n" % {"manifest":myManifestPath})
						self.manifestMissingCache.insert(0,myManifestPath)
			except (OSError, FileNotFound), e:
				if ("strict" in self.mysettings.features) or \
				   ("severe" in self.mysettings.features):
					raise SecurityViolation, "Error in verification of signatures: %(errormsg)s" % {"errormsg":str(e)}
				writemsg("!!! Manifest is missing or inaccessable: %(manifest)s\n" % {"manifest":myManifestPath},
					noiselevel=-1)


		try:
			emtime = os.stat(myebuild)[stat.ST_MTIME]
		except OSError:
			writemsg("!!! aux_get(): ebuild for '%(cpv)s' does not exist at:\n" % {"cpv":mycpv},
				noiselevel=-1)
			writemsg("!!!            %s\n" % myebuild,
				noiselevel=-1)
			raise KeyError(mycpv)

		try:
			mydata = self.auxdb[mylocation][mycpv]
			eapi = mydata.get("EAPI","").strip()
			if not eapi:
				eapi = "0"
			if eapi.startswith("-") and eapi_is_supported(eapi[1:]):
				doregen = True
			elif emtime != long(mydata.get("_mtime_", 0)):
				doregen = True
			elif len(mydata.get("_eclasses_", [])) > 0:
				doregen = not self.eclassdb.is_eclass_data_valid(mydata["_eclasses_"])
			else:
				doregen = False
				
		except KeyError:
			doregen = True
		except CacheError:
			doregen = True
			try:
				del self.auxdb[mylocation][mycpv]
			except KeyError:
				pass

		writemsg("auxdb is valid: "+str(not doregen)+" "+str(pkg)+"\n", 2)

		if doregen:
			if myebuild in self._broken_ebuilds:
				raise KeyError(mycpv)
			if not self._have_root_eclass_dir:
				raise KeyError(mycpv)
			writemsg("doregen: %s %s\n" % (doregen, mycpv), 2)
			writemsg("Generating cache entry(0) for: "+str(myebuild)+"\n", 1)

			self.doebuild_settings.reset()
			mydata = {}
			myret = doebuild(myebuild, "depend",
				self.doebuild_settings["ROOT"], self.doebuild_settings,
				dbkey=mydata, tree="porttree", mydbapi=self)
			if myret != os.EX_OK:
				self._broken_ebuilds.add(myebuild)
				raise KeyError(mycpv)

			if "EAPI" not in mydata or not mydata["EAPI"].strip():
				mydata["EAPI"] = "0"

			if not eapi_is_supported(mydata["EAPI"]):
				# if newer version, wipe everything and negate eapi
				eapi = mydata["EAPI"]
				mydata = {}
				map(lambda x: mydata.setdefault(x, ""), auxdbkeys)
				mydata["EAPI"] = "-"+eapi

			if mydata.get("INHERITED", False):
				mydata["_eclasses_"] = self.eclassdb.get_eclass_data(mydata["INHERITED"].split())
			else:
				mydata["_eclasses_"] = {}
			
			del mydata["INHERITED"]

			mydata["_mtime_"] = emtime

			self.auxdb[mylocation][mycpv] = mydata

		if not mydata.setdefault("EAPI", "0"):
			mydata["EAPI"] = "0"

		# do we have a origin repository name for the current package
		mydata["repository"] = self._repository_map.get(
			os.path.sep.join(myebuild.split(os.path.sep)[:-3]), "")

		#finally, we look at our internal cache entry and return the requested data.
		returnme = []
		for x in mylist:
			if x == "INHERITED":
				returnme.append(' '.join(mydata.get("_eclasses_", [])))
			else:
				returnme.append(mydata.get(x,""))

		if cache_me:
			aux_cache = {}
			for x in self._aux_cache_keys:
				aux_cache[x] = mydata.get(x, "")
			self._aux_cache[mycpv] = aux_cache

		return returnme

	def getfetchlist(self, mypkg, useflags=None, mysettings=None, all=0, mytree=None):
		if mysettings is None:
			mysettings = self.mysettings
		try:
			eapi, myuris = self.aux_get(mypkg,
				["EAPI", "SRC_URI"], mytree=mytree)
		except KeyError:
			# Convert this to an InvalidDependString exception since callers
			# already handle it.
			raise portage.exception.InvalidDependString(
				"getfetchlist(): aux_get() error reading "+mypkg+"; aborting.")

		if not eapi_is_supported(eapi):
			# Convert this to an InvalidDependString exception
			# since callers already handle it.
			raise portage.exception.InvalidDependString(
				"getfetchlist(): '%s' has unsupported EAPI: '%s'" % \
				(mypkg, eapi.lstrip("-")))

		if useflags is None:
			mysettings.setcpv(mypkg, mydb=self)
			useflags = mysettings["PORTAGE_USE"].split()

		myurilist = paren_reduce(myuris)
		myurilist = use_reduce(myurilist, uselist=useflags, matchall=all)
		newuris = flatten(myurilist)

		myfiles = []
		for x in newuris:
			mya = os.path.basename(x)
			if not mya:
				raise portage.exception.InvalidDependString(
					"getfetchlist(): '%s' SRC_URI has no file name: '%s'" % \
					(mypkg, x))
			if not mya in myfiles:
				myfiles.append(mya)
		return [newuris, myfiles]

	def getfetchsizes(self, mypkg, useflags=None, debug=0):
		# returns a filename:size dictionnary of remaining downloads
		myebuild = self.findname(mypkg)
		pkgdir = os.path.dirname(myebuild)
		mf = Manifest(pkgdir, self.mysettings["DISTDIR"])
		checksums = mf.getDigests()
		if not checksums:
			if debug: 
				print "[empty/missing/bad digest]: "+mypkg
			return None
		filesdict={}
		if useflags is None:
			myuris, myfiles = self.getfetchlist(mypkg,all=1)
		else:
			myuris, myfiles = self.getfetchlist(mypkg,useflags=useflags)
		#XXX: maybe this should be improved: take partial downloads
		# into account? check checksums?
		for myfile in myfiles:
			if myfile not in checksums:
				if debug:
					writemsg("[bad digest]: missing %s for %s\n" % (myfile, mypkg))
				continue
			file_path = os.path.join(self.mysettings["DISTDIR"], myfile)
			mystat = None
			try:
				mystat = os.stat(file_path)
			except OSError, e:
				pass
			if mystat is None:
				existing_size = 0
			else:
				existing_size = mystat.st_size
			remaining_size = int(checksums[myfile]["size"]) - existing_size
			if remaining_size > 0:
				# Assume the download is resumable.
				filesdict[myfile] = remaining_size
			elif remaining_size < 0:
				# The existing file is too large and therefore corrupt.
				filesdict[myfile] = int(checksums[myfile]["size"])
		return filesdict

	def fetch_check(self, mypkg, useflags=None, mysettings=None, all=False):
		if not useflags:
			if mysettings:
				useflags = mysettings["USE"].split()
		myuri, myfiles = self.getfetchlist(mypkg, useflags=useflags, mysettings=mysettings, all=all)
		myebuild = self.findname(mypkg)
		pkgdir = os.path.dirname(myebuild)
		mf = Manifest(pkgdir, self.mysettings["DISTDIR"])
		mysums = mf.getDigests()

		failures = {}
		for x in myfiles:
			if not mysums or x not in mysums:
				ok     = False
				reason = "digest missing"
			else:
				try:
					ok, reason = portage.checksum.verify_all(
						os.path.join(self.mysettings["DISTDIR"], x), mysums[x])
				except FileNotFound, e:
					ok = False
					reason = "File Not Found: '%s'" % str(e)
			if not ok:
				failures[x] = reason
		if failures:
			return False
		return True

	def cpv_exists(self, mykey):
		"Tells us whether an actual ebuild exists on disk (no masking)"
		cps2 = mykey.split("/")
		cps = catpkgsplit(mykey, silent=0)
		if not cps:
			#invalid cat/pkg-v
			return 0
		if self.findname(cps[0] + "/" + cps2[1]):
			return 1
		else:
			return 0

	def cp_all(self):
		"returns a list of all keys in our tree"
		d = {}
		for x in self.mysettings.categories:
			for oroot in self.porttrees:
				for y in listdir(oroot+"/"+x, EmptyOnError=1, ignorecvs=1, dirsonly=1):
					if not self._pkg_dir_name_re.match(y) or \
						y == "CVS":
						continue
					d[x+"/"+y] = None
		l = d.keys()
		l.sort()
		return l

	def p_list(self,mycp):
		d={}
		for oroot in self.porttrees:
			for x in listdir(oroot+"/"+mycp,EmptyOnError=1,ignorecvs=1):
				if x[-7:]==".ebuild":
					d[x[:-7]] = None
		return d.keys()

	def cp_list(self, mycp, use_cache=1, mytree=None):
		if self.frozen and mytree is None:
			cachelist = self.xcache["cp-list"].get(mycp)
			if cachelist is not None:
				# Try to propagate this to the match-all cache here for
				# repoman since he uses separate match-all caches for each
				# profile (due to old-style virtuals). Do not propagate
				# old-style virtuals since cp_list() doesn't expand them.
				if not (not cachelist and mycp.startswith("virtual/")):
					self.xcache["match-all"][mycp] = cachelist
				return cachelist[:]
		mysplit = mycp.split("/")
		invalid_category = mysplit[0] not in self._categories
		d={}
		if mytree:
			mytrees = [mytree]
		else:
			mytrees = self.porttrees
		for oroot in mytrees:
			try:
				file_list = os.listdir(os.path.join(oroot, mycp))
			except OSError:
				continue
			for x in file_list:
				if x.endswith(".ebuild"):
					pf = x[:-7]
					ps = pkgsplit(pf)
					if not ps:
						writemsg("\nInvalid ebuild name: %s\n" % \
							os.path.join(oroot, mycp, x), noiselevel=-1)
						continue
					if ps[0] != mysplit[1]:
						writemsg("\nInvalid ebuild name: %s\n" % \
							os.path.join(oroot, mycp, x), noiselevel=-1)
						continue
					d[mysplit[0]+"/"+pf] = None
		if invalid_category and d:
			writemsg(("\n!!! '%s' has a category that is not listed in " + \
				"/etc/portage/categories\n") % mycp, noiselevel=-1)
			mylist = []
		else:
			mylist = d.keys()
		# Always sort in ascending order here since it's handy
		# and the result can be easily cached and reused.
		self._cpv_sort_ascending(mylist)
		if self.frozen and mytree is None:
			cachelist = mylist[:]
			self.xcache["cp-list"][mycp] = cachelist
			# Do not propagate old-style virtuals since
			# cp_list() doesn't expand them.
			if not (not cachelist and mycp.startswith("virtual/")):
				self.xcache["match-all"][mycp] = cachelist
		return mylist

	def freeze(self):
		for x in "bestmatch-visible", "cp-list", "list-visible", "match-all", \
			"match-visible", "minimum-all", "minimum-visible":
			self.xcache[x]={}
		self.frozen=1

	def melt(self):
		self.xcache = {}
		self.frozen = 0

	def xmatch(self,level,origdep,mydep=None,mykey=None,mylist=None):
		"caching match function; very trick stuff"
		#if no updates are being made to the tree, we can consult our xcache...
		if self.frozen:
			try:
				return self.xcache[level][origdep][:]
			except KeyError:
				pass

		if not mydep:
			#this stuff only runs on first call of xmatch()
			#create mydep, mykey from origdep
			mydep = dep_expand(origdep, mydb=self, settings=self.mysettings)
			mykey = dep_getkey(mydep)

		myslot = dep_getslot(mydep)
		if level == "list-visible":
			#a list of all visible packages, not called directly (just by xmatch())
			#myval = self.visible(self.cp_list(mykey))

			myval = self.gvisible(self.visible(self.cp_list(mykey)))
		elif level == "minimum-all":
			# Find the minimum matching version. This is optimized to
			# minimize the number of metadata accesses (improves performance
			# especially in cases where metadata needs to be generated).
			if mydep == mykey:
				mylist = self.cp_list(mykey)
			else:
				mylist = match_from_list(mydep, self.cp_list(mykey))
			myval = ""
			if mylist:
				if myslot is None:
					myval = mylist[0]
				else:
					for cpv in mylist:
						try:
							if self.aux_get(cpv, ["SLOT"])[0] == myslot:
								myval = cpv
								break
						except KeyError:
							pass # ebuild masked by corruption
		elif level in ("minimum-visible", "bestmatch-visible"):
			# Find the minimum matching visible version. This is optimized to
			# minimize the number of metadata accesses (improves performance
			# especially in cases where metadata needs to be generated).
			if mydep == mykey:
				mylist = self.cp_list(mykey)
			else:
				mylist = match_from_list(mydep, self.cp_list(mykey))
			myval = ""
			settings = self.mysettings
			local_config = settings.local_config
			aux_keys = list(self._aux_cache_keys)
			if level == "minimum-visible":
				iterfunc = iter
			else:
				iterfunc = reversed
			for cpv in iterfunc(mylist):
				try:
					metadata = dict(izip(aux_keys,
						self.aux_get(cpv, aux_keys)))
				except KeyError:
					# ebuild masked by corruption
					continue
				if not eapi_is_supported(metadata["EAPI"]):
					continue
				if myslot and myslot != metadata["SLOT"]:
					continue
				if settings.getMissingKeywords(cpv, metadata):
					continue
				if settings.getMaskAtom(cpv, metadata):
					continue
				if settings.getProfileMaskAtom(cpv, metadata):
					continue
				if local_config:
					metadata["USE"] = ""
					if "?" in metadata["LICENSE"]:
						self.doebuild_settings.setcpv(cpv, mydb=metadata)
						metadata["USE"] = self.doebuild_settings.get("USE", "")
					try:
						if settings.getMissingLicenses(cpv, metadata):
							continue
					except InvalidDependString:
						continue
				myval = cpv
				break
		elif level == "bestmatch-list":
			#dep match -- find best match but restrict search to sublist
			#no point in calling xmatch again since we're not caching list deps

			myval = best(match_from_list(mydep, mylist))
		elif level == "match-list":
			#dep match -- find all matches but restrict search to sublist (used in 2nd half of visible())

			myval = match_from_list(mydep, mylist)
		elif level == "match-visible":
			#dep match -- find all visible matches
			#get all visible packages, then get the matching ones

			myval = match_from_list(mydep,
				self.xmatch("list-visible", mykey, mydep=mykey, mykey=mykey))
		elif level == "match-all":
			#match *all* visible *and* masked packages
			if mydep == mykey:
				myval = self.cp_list(mykey)
			else:
				myval = match_from_list(mydep, self.cp_list(mykey))
		else:
			print "ERROR: xmatch doesn't handle", level, "query!"
			raise KeyError
		if myslot is not None and isinstance(myval, list):
			slotmatches = []
			for cpv in myval:
				try:
					if self.aux_get(cpv, ["SLOT"])[0] == myslot:
						slotmatches.append(cpv)
				except KeyError:
					pass # ebuild masked by corruption
			myval = slotmatches
		if self.frozen and (level not in ["match-list", "bestmatch-list"]):
			self.xcache[level][mydep] = myval
			if origdep and origdep != mydep:
				self.xcache[level][origdep] = myval
		return myval[:]

	def match(self, mydep, use_cache=1):
		return self.xmatch("match-visible", mydep)

	def visible(self, mylist):
		"""two functions in one.  Accepts a list of cpv values and uses the package.mask *and*
		packages file to remove invisible entries, returning remaining items.  This function assumes
		that all entries in mylist have the same category and package name."""
		if not mylist:
			return []

		db_keys = ["SLOT"]
		visible = []
		getMaskAtom = self.mysettings.getMaskAtom
		getProfileMaskAtom = self.mysettings.getProfileMaskAtom
		for cpv in mylist:
			try:
				metadata = dict(izip(db_keys, self.aux_get(cpv, db_keys)))
			except KeyError:
				# masked by corruption
				continue
			if not metadata["SLOT"]:
				continue
			if getMaskAtom(cpv, metadata):
				continue
			if getProfileMaskAtom(cpv, metadata):
				continue
			visible.append(cpv)
		return visible

	def gvisible(self,mylist):
		"strip out group-masked (not in current group) entries"

		if mylist is None:
			return []
		newlist=[]
		aux_keys = list(self._aux_cache_keys)
		metadata = {}
		local_config = self.mysettings.local_config
		for mycpv in mylist:
			metadata.clear()
			try:
				metadata.update(izip(aux_keys, self.aux_get(mycpv, aux_keys)))
			except KeyError:
				continue
			except PortageException, e:
				writemsg("!!! Error: aux_get('%s', %s)\n" % (mycpv, aux_keys),
					noiselevel=-1)
				writemsg("!!! %s\n" % str(e), noiselevel=-1)
				del e
				continue
			if not eapi_is_supported(metadata["EAPI"]):
				continue
			if self.mysettings.getMissingKeywords(mycpv, metadata):
				continue
			if local_config:
				metadata["USE"] = ""
				if "?" in metadata["LICENSE"]:
					self.doebuild_settings.setcpv(mycpv, mydb=metadata)
					metadata["USE"] = self.doebuild_settings.get("USE", "")
				try:
					if self.mysettings.getMissingLicenses(mycpv, metadata):
						continue
				except InvalidDependString:
					continue
			newlist.append(mycpv)
		return newlist

def close_portdbapi_caches():
	for i in portdbapi.portdbapi_instances:
		i.close_caches()


class portagetree(object):
	def __init__(self, root="/", virtual=None, clone=None, settings=None):
		"""
		Constructor for a PortageTree
		
		@param root: ${ROOT}, defaults to '/', see make.conf(5)
		@type root: String/Path
		@param virtual: UNUSED
		@type virtual: No Idea
		@param clone: Set this if you want a copy of Clone
		@type clone: Existing portagetree Instance
		@param settings: Portage Configuration object (portage.settings)
		@type settings: Instance of portage.config
		"""

		if clone:
			writemsg("portagetree.__init__(): deprecated " + \
				"use of clone parameter\n", noiselevel=-1)
			self.root = clone.root
			self.portroot = clone.portroot
			self.pkglines = clone.pkglines
		else:
			self.root = root
			if settings is None:
				from portage import settings
			self.settings = settings
			self.portroot = settings["PORTDIR"]
			self.virtual = virtual
			self.dbapi = portdbapi(
				settings["PORTDIR"], mysettings=settings)

	def dep_bestmatch(self,mydep):
		"compatibility method"
		mymatch = self.dbapi.xmatch("bestmatch-visible",mydep)
		if mymatch is None:
			return ""
		return mymatch

	def dep_match(self,mydep):
		"compatibility method"
		mymatch = self.dbapi.xmatch("match-visible",mydep)
		if mymatch is None:
			return []
		return mymatch

	def exists_specific(self,cpv):
		return self.dbapi.cpv_exists(cpv)

	def getallnodes(self):
		"""new behavior: these are all *unmasked* nodes.  There may or may not be available
		masked package for nodes in this nodes list."""
		return self.dbapi.cp_all()

	def getname(self, pkgname):
		"returns file location for this particular package (DEPRECATED)"
		if not pkgname:
			return ""
		mysplit = pkgname.split("/")
		psplit = pkgsplit(mysplit[1])
		return "/".join([self.portroot, mysplit[0], psplit[0], mysplit[1]])+".ebuild"

	def resolve_specific(self, myspec):
		cps = catpkgsplit(myspec)
		if not cps:
			return None
		mykey = key_expand(cps[0]+"/"+cps[1], mydb=self.dbapi,
			settings=self.settings)
		mykey = mykey + "-" + cps[2]
		if cps[3] != "r0":
			mykey = mykey + "-" + cps[3]
		return mykey

	def depcheck(self, mycheck, use="yes", myusesplit=None):
		return dep_check(mycheck, self.dbapi, use=use, myuse=myusesplit)

	def getslot(self,mycatpkg):
		"Get a slot for a catpkg; assume it exists."
		myslot = ""
		try:
			myslot = self.dbapi.aux_get(mycatpkg, ["SLOT"])[0]
		except SystemExit, e:
			raise
		except Exception, e:
			pass
		return myslot

