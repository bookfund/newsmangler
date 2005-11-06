# ---------------------------------------------------------------------------
# $Id: poster.py 3875 2005-10-03 08:19:19Z freddie $
# ---------------------------------------------------------------------------
# Copyright (c) 2005, freddie@madcowdisease.org
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Main class for posting stuff."""

import asyncore
import os
import select
import sys
import time

from cStringIO import StringIO

from classes import asyncNNTP
from classes import yEnc
from classes.BaseMangler import BaseMangler
from classes.Common import *

# ---------------------------------------------------------------------------

class Poster(BaseMangler):
	def __init__(self, conf):
		BaseMangler.__init__(self, conf)
		
		self.conf['posting']['skip_filenames'] = self.conf['posting'].get('skip_filenames', '').split()
		
		self._articles = []
		self._files = {}
		self._msgids = {}
		
		self._current_dir = None
		self.newsgroup = None
		self.post_title = None
		self._par2_queue = []
		
		# Some sort of useful logging junk about what yEncode we're using
		self.logger.info('Using %s module for yEnc encoding.', yEnc.yEncMode())
	
	def post(self, newsgroup, postme, post_title=None, generate_par2=False):
		self.newsgroup = newsgroup
		self.post_title = post_title
		
		# Generate the list of articles we need to post
		self.generate_article_list(postme)
		
		# If we have no valid articles, bail
		if not self._articles:
			self.logger.warning('No valid articles to post!')
			return
		
		# Connect!
		self.connect()
		
		# Slight speedup
		_poll = self.poll
		_sleep = time.sleep
		_time = time.time
		
		# Wait until at least one connection is ready
		last_stuff = _time()
		while 1:
			now = _time()
			_poll()
			
			if self._idle:
				break
			
			if now - last_stuff >= 1:
				last_stuff = now
				for conn in self._conns:
					if conn.state == asyncNNTP.STATE_DISCONNECTED and now >= conn.reconnect_at:
						conn.do_connect()
			
			_sleep(0.01)
		
		# And loop
		self._bytes = 0
		start = _time()
		
		self.logger.info('Posting %d articles...', len(self._articles))
		
		while 1:
			now = _time()
			
			# Poll our sockets for events
			_poll()
			
			# Possibly post some more parts now
			while self._idle and self._articles:
				article = self._articles.pop(0)
				postfile = StringIO()
				self.build_article(postfile, article)
				
				conn = self._idle.pop(0)
				conn.post_article(postfile)
			
			# Do some stuff roughly once per second
			if now - last_stuff >= 1:
				last_stuff = now
				
				for conn in self._conns:
					if conn.state == asyncNNTP.STATE_DISCONNECTED and now >= conn.reconnect_at:
						conn.do_connect()
				
				if self._bytes:
					interval = time.time() - start
					speed = self._bytes / interval / 1024
					left = len(self._articles) + (len(self._conns) - len(self._idle))
					print '%d articles remaining - %.1fKB/s     \r' % (left, speed),
					sys.stdout.flush()
			
			# All done?
			if self._articles == [] and len(self._idle) == self.conf['server']['connections']:
				interval = time.time() - start
				speed = self._bytes / interval / 1024
				self.logger.info('Posting complete - %d bytes in %.2fs (%.1fKB/s)',
					self._bytes, interval, speed)
				
				# If we have some msgids left over, we might have to generate
				# a .NZB
				if self.conf['posting']['generate_nzbs'] and self._msgids:
					self.generate_nzb()
				
				return
			
			# And sleep for a bit to try and cut CPU chompage
			_sleep(0.01)
	
	# -----------------------------------------------------------------------
	# Generate the list of articles we need to post
	def generate_article_list(self, postme):
		# "files" mode is just one lot of files
		if self.post_title:
			self._gal_files(self.post_title, postme)
		# "dirs" mode could be a whole bunch
		else:
			for dirname in postme:
				if dirname.endswith(os.sep):
					dirname = dirname[:-len(os.sep)]
				if not dirname:
					continue
				
				self._gal_files(os.path.basename(dirname), os.listdir(dirname), basepath=dirname)
		
		# Debug junk
		if 0:
			for article in self._articles:
				print article[1]
	
	# Do the heavy lifting for generate_article_list
	def _gal_files(self, post_title, files, basepath=''):
		article_size = self.conf['posting']['article_size']
		
		goodfiles = []
		for filename in files:
			filepath = os.path.abspath(os.path.join(basepath, filename))
			
			# Skip non-files and empty files
			if not os.path.isfile(filepath):
				continue
			if filename in self.conf['posting']['skip_filenames']:
				continue
			filesize = os.path.getsize(filepath)
			if not filesize:
				continue
			
			goodfiles.append((filename, filepath, filesize))
		goodfiles.sort()
		
		n = 1
		for filename, filepath, filesize in goodfiles:
			full, partial = divmod(filesize, article_size)
			if partial:
				parts = full + 1
			else:
				parts = full
			
			# Build a subject
			real_filename = os.path.split(filename)[1]
			
			temp = '%%0%sd' % (len(str(len(files))))
			filenum = temp % (n)
			temp = '%%0%sd' % (len(str(parts)))
			subject = '%s [%s/%d] - "%s" yEnc (%s/%d)' % (
				post_title, filenum, len(goodfiles), real_filename, temp, parts
			)
			
			if self.conf['posting']['subject_prefix']:
				subject = '%s %s' % (self.conf['posting']['subject_prefix'], subject)
			
			# Now make up our parts
			fileinfo = {
				'dirname': post_title,
				'filename': real_filename,
				'filepath': filepath,
				'filesize': filesize,
				'parts': parts,
			}
			
			for i in range(parts):
				article = [fileinfo, subject, i+1]
				self._articles.append(article)
			
			n += 1
	
	# -----------------------------------------------------------------------
	# Build an article for posting.
	def build_article(self, postfile, article):
		(fileinfo, subject, partnum) = article
		
		# Read the chunk of data from the file
		f = self._files.get(fileinfo['filepath'], None)
		if f is None:
			self._files[fileinfo['filepath']] = f = open(fileinfo['filepath'], 'rb')
		
		begin = f.tell()
		data = f.read(self.conf['posting']['article_size'])
		end = f.tell()
		
		# If that was the last part, close the file and throw it away
		if partnum == fileinfo['parts']:
			self._files[fileinfo['filepath']].close()
			del self._files[fileinfo['filepath']]
		
		# Basic headers
		line = 'From: %s\r\n' % (self.conf['posting']['from'])
		postfile.write(line)
		
		line = 'Newsgroups: %s\r\n' % (self.newsgroup)
		postfile.write(line)
		
		#line = time.strftime('Date: %a, %d %b %Y %H:%M:%S GMT\r\n', time.gmtime())
		#postfile.write(line)
		
		subj = subject % (partnum)
		line = 'Subject: %s\r\n' % (subj)
		postfile.write(line)
		
		msgid = '%.5f,%d@%s' % (time.time(), partnum, self.conf['server']['hostname'])
		line = 'Message-ID: <%s>\r\n' % (msgid)
		postfile.write(line)
		
		line = 'X-Newsposter: newsmangler %s (%s) - http://www.madcowdisease.org/mcd/newsmangler\r\n' % (
			NM_VERSION, yEnc.yEncMode())
		postfile.write(line)
		
		postfile.write('\r\n')
		
		# yEnc start
		line = '=ybegin part=%d total=%d line=256 size=%d name=%s\r\n' % (
			partnum, fileinfo['parts'], fileinfo['filesize'], fileinfo['filename']
		)
		postfile.write(line)
		line = '=ypart begin=%d end=%d\r\n' % (begin+1, end)
		postfile.write(line)
		
		# yEnc data
		partcrc = yEnc.yEncode(postfile, data)
		
		# yEnc end
		line = '=yend size=%d part=%d pcrc32=%s\r\n' % (end-begin, partnum, partcrc)
		postfile.write(line)
		
		# And done writing for now
		postfile.write('.\r\n')
		article_size = postfile.tell()
		postfile.seek(0, 0)
		
		# Maybe remember the msgid for later
		if self.conf['posting']['generate_nzbs']:
			if self._current_dir != fileinfo['dirname']:
				if self._msgids:
					self.generate_nzb()
					self._msgids = {}
				
				self._current_dir = fileinfo['dirname']
			
			subj = subject % (1)
			if subj not in self._msgids:
				self._msgids[subj] = [int(time.time())]
			self._msgids[subj].append((msgid, article_size))
	
	# -----------------------------------------------------------------------
	# Generate a .NZB file!
	def generate_nzb(self):
		filename = 'newsmangler_%s.nzb' % (SafeFilename(self._current_dir))
		nzbfile = open(filename, 'w')
		
		nzbfile.write('<?xml version="1.0" encoding="iso-8859-1" ?>\n')
		nzbfile.write('<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.0//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.0.dtd">\n')
		nzbfile.write('<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n')
		
		for subject, msgids in self._msgids.items():
			posttime = msgids.pop(0)
			
			line = '  <file poster="%s" date="%s" subject="%s">\n' % (
				XMLBrackets(self.conf['posting']['from']), posttime,
				XMLBrackets(subject)
			)
			nzbfile.write(line)
			nzbfile.write('    <groups>\n')
			
			for newsgroup in self.newsgroup.split(','):
				line = '      <group>%s</group>\n' % (newsgroup)
				nzbfile.write(line)
			
			nzbfile.write('    </groups>\n')
			nzbfile.write('    <segments>\n')
			
			for i, (msgid, article_size) in enumerate(msgids):
				line = '      <segment bytes="%s" number="%s">%s</segment>\n' % (
					article_size, i+1, msgid
				)
				nzbfile.write(line)
			
			nzbfile.write('    </segments>\n')
			nzbfile.write('  </file>\n')
		
		gentime = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
		line = '\n  <!-- Generated by newsmangler at %s -->\n' % (gentime)
		nzbfile.write(line)
		
		nzbfile.write('</nzb>\n')
		nzbfile.close()
		
		self.logger.info('Generated %s', filename)

# ---------------------------------------------------------------------------
