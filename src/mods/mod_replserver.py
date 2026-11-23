# -*- coding: utf-8 -*-
'''mod_replserver - live TCP REPL into the running 0.8.2 client.

GPL port of juho-p/wot-debugserver (https://github.com/juho-p/wot-debugserver)
adapted for the CameraNode GUI-mod loader (plain .py file, no XFW).

On game start it opens 127.0.0.1:2222 (the visible client) - the headless
authority worker started by Start_Offline_With_Worker.bat opens 2223 instead, so
a connection to 2222 always means the client you are looking at. Connect with
netcat/telnet from anywhere local (WSL: nc 127.0.0.1 2222 - but see note) or the
bundled client.py from the original repo. Every line is compiled in 'single' mode
(interactive): expressions auto-print, statements run, blocks work - end a
compound header with ':' and keep feeding lines until it executes. QUIT
closes the session. Namespace persists between commands.
'''
import datetime
import os
import socket
import threading
import traceback
import sys
import cStringIO

# Why the port depends on the process role: SO_REUSEADDR is set below, and on
# Windows that lets a SECOND socket bind a port that is already bound instead of
# failing - so the worker (which the launcher starts before the client) and the
# client both ended up listening on 2222, and an incoming connection was served
# by whichever socket the stack picked. Measured 04.10: a probe sent while the
# visible client was in battle answered 'PROCESS worker_mode=True', i.e. every
# reading came from the wrong process (this is exactly the "the REPL will report
# things that are not in the client" failure the worker plan was supposed to
# avoid). Separate ports make the reading unambiguous, and 2223 also gives the
# worker its own debug channel.
_PORT = (2223 if os.environ.get('OFFHANGAR_WORKER_MODE') == 'authority_worker'
         else 2222)
_G = {}


def _log(text):
    try:
        ds = datetime.datetime.now().strftime('%H:%M')
        print 'replserver %s: %s' % (ds, text)
    except Exception:
        pass


class _Session(object):
    def __init__(self, conn):
        self.conn = conn
        self.buf = ''
        self.pending = ''

    def _send(self, text):
        try:
            self.conn.sendall(text.replace('\n', '\r\n') + '\r\n')
        except Exception:
            pass

    def _execute(self, source):
        out = cStringIO.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            try:
                code = compile(source, '<repl>', 'single')
                exec code in _G
            except Exception:
                traceback.print_exc()
        finally:
            sys.stdout = old
        text = out.getvalue().rstrip()
        self._send(text if text else '(ok)')

    def run(self):
        self._send('~~~ok!~~~ 0.8.2 repl ready')
        while True:
            try:
                data = self.conn.recv(65536)
            except Exception:
                return
            if not data:
                return
            self.buf += data
            while '\n' in self.buf:
                line, self.buf = self.buf.split('\n', 1)
                line = line.rstrip('\r').strip()
                if line == 'QUIT':
                    self._send('bye')
                    return
                if not line and not self.pending:
                    continue
                candidate = (self.pending + '\n' + line) if self.pending else line
                try:
                    compile(candidate, '<repl>', 'single')
                except SyntaxError as se:
                    msg = str(se)
                    if ('unexpected EOF' in msg or
                            'expected an indented block' in msg or
                            'after line continuation character' in msg):
                        self.pending = candidate
                        continue
                    self.pending = ''
                    self._execute(candidate)
                else:
                    self.pending = ''
                    self._execute(candidate)


def _server_main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('127.0.0.1', _PORT))
    except Exception:
        _log('bind failed on %d (already running?)' % _PORT)
        return
    s.listen(2)
    _log('listening on 127.0.0.1:%d' % _PORT)
    while True:
        try:
            conn, addr = s.accept()
        except Exception:
            continue
        _log('connection from %s' % (addr,))
        try:
            _Session(conn).run()
        except Exception:
            traceback.print_exc()
        finally:
            try:
                conn.close()
            except Exception:
                pass


_t = threading.Thread(target=_server_main)
_t.setDaemon(True)
_t.start()
