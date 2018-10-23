import sublime
import sublime_plugin

import subprocess
import threading
import os

class ClangCompileAsmCommand(sublime_plugin.WindowCommand):
  encoding = 'utf-8'
  proc = None
  panel = None
  panel_lock = threading.Lock()

  def is_enabled(self, arch=None, sdk=None, extra_args=None):
    return True

  def run(self, arch=None, sdk=None, extra_args=None):
    active_view = self.window.active_view()
    vars = self.window.extract_variables()
    working_dir = vars['file_path']

    settings = sublime.load_settings('Compile Assembly.sublime-settings')
    if settings is None:
      return

    file_name = vars['file_name']
    (base_file_name, file_extension) = os.path.splitext(file_name)
    asm_file_name = '%s.%s.asm' % (base_file_name, arch)

    if file_extension in ['.m', '.mm']:
      use_objc_arc = '-fno-objc-arc' not in extra_args
    else:
      use_objc_arc = False

    compile_options = settings.get("compile_options%s" % (file_extension))

    use_modules = active_view.find(r'^\s*@import\b', 0) is not None

    # A lock is used to ensure only one thread is
    # touching the output panel at a time
    with self.panel_lock:
      self.panel = self.window.find_open_file(asm_file_name)
      if self.panel is None:
        self.panel = self.window.new_file()
        self.panel.set_name(asm_file_name)
        self.panel.set_scratch(True)

      syntax_file = settings.get('syntax_file.%s' % (arch))
      if syntax_file is not None:
        self.panel.set_syntax_file(syntax_file)

    if self.proc is not None:
      self.proc.terminate()
      self.proc = None

    args = ['xcrun']
    if sdk is not None:
      args.extend(['--sdk', sdk])
    args.append('clang')
    if arch is not None:
      if arch == 'llvm':
        args.extend(['-arch', 'arm64', '-emit-llvm'])
      else:
        args.extend(['-arch', arch])

    if use_objc_arc:
      args.append('-fobjc-arc')
    if use_modules:
      args.append('-fmodules')
    optimization_level = settings.get('optimization_level', '-Os')
    if optimization_level is not None:
      args.append(optimization_level)
    if extra_args is not None:
      args.extend(extra_args)
    if compile_options is not None:
      args.extend(compile_options)
    args.append('-S')
    args.append('-o-')
    args.append('-')
    # args.append(vars['file_name'])

    self.proc = subprocess.Popen(
      args,
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      cwd=working_dir
    )
    self.killed = False

    current_file_text = active_view.substr(sublime.Region(0, active_view.size()))
    self.do_write('; Compiled with: "%s"\n; -- Piping %d bytes to compiler\n\n' % (' '.join(args), len(current_file_text)))

    threading.Thread(
      target=self.write_handle,
      args=(self.proc.stdin, current_file_text,)
    ).start()

    threading.Thread(
      target=self.read_handle,
      args=(self.proc.stdout,)
    ).start()

  def write_handle(self, handle, file_text):
    try:
      os.write(handle.fileno(), file_text.encode(self.encoding))
      os.close(handle.fileno())
    except (UnicodeEncodeError) as e:
      msg = 'Error decoding input using %s - %s'
      self.queue_write(msg % (self.encoding, str(e)))

  def read_handle(self, handle):
    chunk_size = 2 ** 13
    out = b''
    while True:
      try:
        data = os.read(handle.fileno(), chunk_size)
        out += data
        if len(data) == chunk_size:
          continue
        if data == b'' and out == b'':
          raise IOError('EOF')
        self.queue_write(out.decode(self.encoding))
        if data == b'':
          raise IOError('EOF')
        out = b''
      except (UnicodeDecodeError) as e:
        msg = 'Error decoding output using %s - %s'
        self.queue_write(msg  % (self.encoding, str(e)))
        break
      except (IOError):
        break

  def queue_write(self, text):
    sublime.set_timeout(lambda: self.do_write(text), 1)

  def do_write(self, text):
    with self.panel_lock:
      self.panel.run_command('content_append', {'text': text})

class ContentAppend(sublime_plugin.TextCommand):
  def run(self, edit, text):
    loc = self.view.size()
    self.view.insert(edit, loc, text)
