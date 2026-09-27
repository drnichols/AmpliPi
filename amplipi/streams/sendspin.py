from .base_streams import PersistentStream, logger
from typing import ClassVar, Optional
from amplipi import models, utils
from amplipi.mpris import MPRIS
import subprocess
import os
import hashlib
import json
import socket
import sys
import time

# installed by scripts/configure.py via `uv tool install sendspin` (requires python >= 3.12, so it lives outside our venv)
SENDSPIN_BIN = os.path.expanduser('~/.local/bin/sendspin')


class Sendspin(PersistentStream):
  """ A Music Assistant Sendspin player using the sendspin CLI daemon """

  stream_type: ClassVar[str] = 'sendspin'
  DEFAULT_SERVER_PORT: ClassVar[int] = 8927
  # Music Assistant's server listens on 8927, keep our client listeners clear of it in case MA runs on this box
  LISTEN_PORT_BASE: ClassVar[int] = 8930

  def __init__(self, name: str, server: Optional[str] = None, port: Optional[int] = None, disabled: bool = False, mock: bool = False):
    super().__init__(self.stream_type, name, disabled=disabled, mock=mock)
    self.server: Optional[str] = server
    self.port: Optional[int] = port
    self.mpris: Optional[MPRIS] = None
    self._dbus_proc: Optional[subprocess.Popen] = None
    self.supported_cmds = ['play', 'pause', 'next', 'prev']

  @staticmethod
  def is_available() -> bool:
    """ Is the sendspin client installed? """
    return os.path.exists(SENDSPIN_BIN)

  def is_persistent(self):
    return True

  def reconfig(self, **kwargs):
    reconnect_needed = False
    if 'disabled' in kwargs:
      self.disabled = kwargs['disabled']
    if 'name' in kwargs and kwargs['name'] != self.name:
      self.name = kwargs['name']
      reconnect_needed = True
    if 'server' in kwargs and kwargs['server'] != self.server:
      self.server = kwargs['server']
      reconnect_needed = True
    if 'port' in kwargs and kwargs['port'] != self.port:
      self.port = kwargs['port']
      reconnect_needed = True
    if reconnect_needed and self.is_activated():
      self.reactivate()

  def server_url(self) -> Optional[str]:
    """ Websocket url of the Music Assistant server, None to let the server discover us via mDNS """
    if not self.server:
      return None
    if self.server.startswith(('ws://', 'wss://')):
      return self.server
    return f'ws://{self.server}:{self.port or self.DEFAULT_SERVER_PORT}/sendspin'

  def client_id(self) -> str:
    """ Stable, unique sendspin client id. sendspin defaults to sendspin-cli-<hostname>, which every
    stream on this box would share, making the server treat them as one player """
    name_hash = hashlib.md5(self.name.encode('utf-8')).hexdigest()[:8]
    return f'amplipi-{socket.gethostname()}-{name_hash}'

  def _activate(self, vsrc: int):
    """ Start a sendspin player that outputs to the given virtual source """
    src_config_folder = f'{utils.get_folder("config")}/srcs/v{vsrc}'
    os.makedirs(src_config_folder, exist_ok=True)
    # keyed by client id (not vsrc) so pairing records and the player volume persist across restarts
    settings_dir = f'{utils.get_folder("config")}/sendspin/{self.client_id()}'
    os.makedirs(settings_dir, exist_ok=True)
    settings_file = f'{settings_dir}/settings-daemon.json'
    if not os.path.exists(settings_file):
      # sendspin's software volume defaults to 25%, start at full scale and let the zones set the volume
      with open(settings_file, 'w', encoding='utf-8') as f:
        json.dump({'player_volume': 100}, f)

    # sendspin registers its MPRIS service as org.mpris.MediaPlayer2.Sendspin.instance<pid>, give each instance
    # a private session bus so the service found there is unambiguously this stream's
    bus_path = os.path.join(os.environ.get('XDG_RUNTIME_DIR', src_config_folder), f'amplipi-sendspin-v{vsrc}.bus')
    bus_address = f'unix:path={bus_path}'
    try:
      os.remove(bus_path)
    except FileNotFoundError:
      pass
    self._dbus_proc = subprocess.Popen(args=['dbus-daemon', '--session', '--nofork', f'--address={bus_address}'])
    for _ in range(20):  # the socket shows up asynchronously, give it a moment before anything connects
      if os.path.exists(bus_path):
        break
      time.sleep(0.1)

    args = [
      sys.executable, f'{utils.get_folder("streams")}/process_monitor.py',
      SENDSPIN_BIN, 'daemon',
      '--id', self.client_id(),
      '--name', self.name,
      '--audio-device', utils.virtual_output_device(vsrc),
      '--port', str(self.LISTEN_PORT_BASE + vsrc),
      '--settings-dir', settings_dir,
      '--hardware-volume', 'false',  # zone volume is handled by AmpliPi
    ]
    url = self.server_url()
    if url:
      args += ['--url', url]
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=bus_address)
    logger.info(f'sendspin args: {args}')
    self.proc = subprocess.Popen(args=args, env=env)

    try:
      self.mpris = MPRIS('Sendspin.*', f'{src_config_folder}/sendspin_metadata.json', bus_address=bus_address)
    except Exception as exc:
      logger.exception(f'Error starting sendspin MPRIS reader: {exc}')

  def _deactivate(self):
    if self.mpris:
      self.mpris.close()
    self.mpris = None
    for proc in (self.proc, self._dbus_proc):
      if proc is not None and proc.poll() is None:
        try:
          proc.terminate()
          proc.communicate(timeout=10)
        except Exception as e:
          logger.exception(f'failed to gracefully terminate sendspin stream {self.name}: {e}')
          proc.kill()
          proc.communicate(timeout=3)
    self.proc = None
    self._dbus_proc = None

  def info(self) -> models.SourceInfo:
    source = models.SourceInfo(
      name=self.full_name(),
      state=self.state,
      img_url='static/imgs/sendspin.svg',
      type=self.stream_type
    )
    if not self.mpris:
      return source
    try:
      md = self.mpris.metadata()
      if md.state == 'Playing':
        source.state = 'playing'
      elif md.state == 'Paused':
        source.state = 'paused'
      else:
        source.state = 'stopped'
      if source.state != 'stopped':
        source.artist = md.artist
        source.track = md.title
        source.album = md.album
        source.supported_cmds = list(self.supported_cmds)
    except Exception as e:
      logger.exception(f'error reading sendspin metadata: {e}')
    return source

  def send_cmd(self, cmd):
    if cmd not in self.supported_cmds:
      raise NotImplementedError(f'"{cmd}" is either incorrect or not currently supported')
    if not self.mpris:
      raise Exception(f'{self.name} is not running')
    if cmd == 'play':
      self.mpris.play()
    elif cmd == 'pause':
      self.mpris.pause()
    elif cmd == 'next':
      self.mpris.next()
    elif cmd == 'prev':
      self.mpris.previous()
