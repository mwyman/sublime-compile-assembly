import sublime
import sublime_plugin

import re
import subprocess
import tempfile
import threading
import os

class ClangCompileAsmCommand(sublime_plugin.WindowCommand):
  encoding = 'utf-8'
  proc = None
  panel = None
  panel_lock = threading.Lock()
  cfi_re = re.compile(r'''$\s*\.(loh|cfi_).*$''', re.MULTILINE)

  def is_enabled(self, arch=None, sdk=None, extra_args=None, device_os=None):
    return True

  def run(self, arch=None, sdk=None, extra_args=None, device_os=None):
    active_view = self.window.active_view()
    vars = self.window.extract_variables()

    settings = sublime.load_settings('Compile Assembly.sublime-settings')
    if settings is None:
      return

    if 'file_name' in vars:
      file_name = vars['file_name']
      working_dir = vars['file_path']
    else:
      # If the buffer being compiled is unnamed, give it a default name, using
      # the most permissive compile options (ObjC++).
      file_name = active_view.name() if active_view.name() else 'untitled.mm'
      working_dir = tempfile.gettempdir()

    (base_file_name, file_extension) = os.path.splitext(file_name)
    asm_file_name = '%s.%s.asm' % (base_file_name, arch)

    if file_extension in ['.m', '.mm']:
      use_objc_arc = '-fno-objc-arc' not in extra_args
    else:
      use_objc_arc = False

    compile_options = settings.get("compile_options%s" % (file_extension))

    use_modules = self.shouldUseModules(active_view)

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

    if settings.has('clang_path') and os.path.exists(settings.get('clang_path')):
      # Support for custom builds of clang.
      args.append(settings.get('clang_path'))

      # Custom builds of clang may need to provide an -isysroot for Apple
      # framework paths.
      if settings.has('clang_sysroot'):
        args.extend(['-isysroot', settings.get('clang_sysroot')])
    else:
      args.append('clang')

    if arch is not None:
      if arch == 'llvm':
        args.extend(['-arch', 'arm64', '-emit-llvm'])
      elif device_os is not None:
        args.extend(['-target', self.getTarget(sdk, arch, device_os)])
      else:
        args.extend(['-arch', arch])

    if use_objc_arc:
      args.append('-fobjc-arc')
    if use_modules:
      args.append('-fmodules')
    optimization_level = settings.get('optimization_level', '-Os')
    if optimization_level is not None:
      args.append(optimization_level)
    if not self.skipStandardWarnings(active_view):
      compile_warning_flags = settings.get('compile_warning_flags', [])
      if compile_warning_flags:
        args.extend(compile_warning_flags)
    if extra_args is not None:
      args.extend(extra_args)
    if compile_options is not None:
      args.extend(compile_options)
    else:
      # As a fallback if no compile options are found, default to Objective-C++.
      args.extend(['-x', 'objective-c++', '-std=c++11'])
    args.extend(self.fileCompileArguments(active_view))

    output_type = self.getOutputType(active_view)
    if output_type:
      args.extend(output_type)
    else:
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
    self.do_write('; Compiled with: %s\n' % (' '.join(args)))
    self.do_write('; -- Piped %d bytes to compiler\n' % (len(current_file_text)))

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
    text = self.cfi_re.sub('', text)
    sublime.set_timeout(lambda: self.do_write(text), 1)

  def do_write(self, text):
    with self.panel_lock:
      self.panel.run_command('content_append', {'text': text})

  def getTarget(self, sdk, arch, device_os):
    output = subprocess.check_output(['xcrun', '--sdk', sdk, '--show-sdk-platform-version'])
    os_version = output.decode('utf-8').rstrip()
    return "{arch}-apple-{os}{os_version}".format(arch=arch, os=device_os, os_version=os_version)

  def skipStandardWarnings(self, view):
    region = view.find(r'sublime-compile-assembly-skip-warnings', 0)
    return region is not None and not region.empty()

  def getOutputType(self, view):
    region = view.find(r'sublime-compile-assembly-output:\s*[^\n]*', 0):
    if region is not None and not region.empty()
      return view.substr(region).strip().split()
    return None

  def fileCompileArguments(self, view):
    args = []
    for region in view.find_all(r'sublime-compile-assembly-args:\s*[^\n]*', 0):
      arg_string = view.substr(region)[30:].strip()
      if len(arg_string) > 0:
        args.extend(arg_string.split())
    return args

  def shouldUseModules(self, view):
    region = view.find(r'^\s*@import\b', 0)
    return region is not None and not region.empty()

class ContentAppend(sublime_plugin.TextCommand):
  def run(self, edit, text):
    loc = self.view.size()
    self.view.insert(edit, loc, text)
