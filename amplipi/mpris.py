"""A module for interfacing with an MPRIS MediaPlayer2 over dbus."""

from dataclasses import dataclass
from enum import Enum, auto
import json
import os
import sys
import logging
from typing import List, Optional
import subprocess
from dasbus.connection import SessionMessageBus, AddressedMessageBus
from dasbus.client.proxy import disconnect_proxy
from amplipi import utils

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
sh = logging.StreamHandler(sys.stdout)
logger.addHandler(sh)


class CommandTypes(Enum):
  PLAY = auto()
  PAUSE = auto()
  NEXT = auto()
  PREVIOUS = auto()


@dataclass
class Metadata:
  """A data class for storing metadata on a song."""
  artist: str = ''
  title: str = ''
  art_url: str = ''
  album: str = ''
  state: str = ''
  connected: bool = False
  state_changed_time: float = 0


class MPRIS:
  """A class for interfacing with an MPRIS MediaPlayer2 over dbus."""

  def __init__(self, service_suffix, metadata_path, bus_address: Optional[str] = None) -> None:
    """ service_suffix may end in '*' to match the first service with that prefix (e.g. 'Sendspin.*' for
    players that register as org.mpris.MediaPlayer2.Sendspin.instance<pid>).
    bus_address selects a private dbus session bus, the default session bus is used when None """
    self._bus = AddressedMessageBus(bus_address) if bus_address else SessionMessageBus()
    self._resolved_name: Optional[str] = None
    self.mpris = None if service_suffix.endswith('*') else self._get_proxy(f"org.mpris.MediaPlayer2.{service_suffix}")

    self.capabilities: List[CommandTypes] = []

    self.service_suffix = service_suffix
    self.metadata_path = metadata_path
    self._closing = False

    try:
      with open(self.metadata_path, "w", encoding='utf-8') as f:
        m = Metadata()
        m.state = "Stopped"
        json.dump(m.__dict__, f)
    except Exception as e:
      logger.exception(f'Exception clearing metadata file: {e}')

    try:
      child_args = [sys.executable,
                    f"{utils.get_folder('streams')}/MPRIS_metadata_reader.py",
                    self.service_suffix,
                    self.metadata_path]

      env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=bus_address) if bus_address else None
      self.metadata_process = subprocess.Popen(args=child_args, stdout=sys.stdout, stderr=sys.stderr, env=env)
    except Exception as e:
      logger.exception(f'Exception starting MPRIS metadata process: {e}')

  def _get_proxy(self, service_name):
    return self._bus.get_proxy(
      service_name=service_name,
      object_path="/org/mpris/MediaPlayer2",
      interface_name="org.mpris.MediaPlayer2.Player"
    )

  def _player(self):
    """ The player proxy, resolving a wildcard service_suffix to the currently running instance """
    if self.service_suffix.endswith('*'):
      prefix = f"org.mpris.MediaPlayer2.{self.service_suffix[:-1]}"
      name = next((n for n in self._bus.proxy.ListNames() if n.startswith(prefix)), None)
      if name is None:
        raise Exception(f'no MPRIS service matching {prefix}*')
      if name != self._resolved_name:
        if self.mpris:
          disconnect_proxy(self.mpris)
        self.mpris = self._get_proxy(name)
        self._resolved_name = name
    return self.mpris

  def play(self) -> None:
    """Plays."""
    self._player().Play()

  def pause(self) -> None:
    """Pauses."""
    self._player().Pause()

  def next(self) -> None:
    """Skips song."""
    self._player().Next()

  def previous(self) -> None:
    """Goes back a song."""
    self._player().Previous()

  def play_pause(self) -> None:
    """Plays or pauses depending on current state."""
    self._player().PlayPause()

  def _load_metadata(self) -> Metadata:
    try:
      with open(self.metadata_path, 'r', encoding='utf-8') as f:
        metadata_dict = json.load(f)
        metadata_obj = Metadata()

        for k in metadata_dict.keys():
          metadata_obj.__dict__[k] = metadata_dict[k]

        return metadata_obj
    except Exception as e:
      logger.exception(f"MPRIS loading metadata at {self.metadata_path} failed: {e}")

    return Metadata()

  def metadata(self) -> Metadata:
    """Returns metadata from MPRIS."""
    return self._load_metadata()

  def is_playing(self) -> bool:
    """Playing?"""
    return self._load_metadata().state == 'Playing'

  def is_stopped(self) -> bool:
    """Stopped?"""
    return self._load_metadata().state == 'Stopped'

  def is_connected(self) -> bool:
    """Returns true if we can talk to the MPRIS dbus object."""
    return self._load_metadata().connected

  def get_capabilities(self) -> List[CommandTypes]:
    """Returns a list of supported commands."""

    if len(self.capabilities) == 0:

      if self._player().CanPlay:
        self.capabilities.append(CommandTypes.PLAY)

      if self._player().CanPause:
        self.capabilities.append(CommandTypes.PAUSE)

      if self._player().CanGoNext:
        self.capabilities.append(CommandTypes.NEXT)

      if self._player().CanGoPrevious:
        self.capabilities.append(CommandTypes.PREVIOUS)

    return self.capabilities

  def close(self):
    """Closes the MPRIS object."""

    if self.metadata_process:
      self.metadata_process.terminate()
      if self.metadata_process.wait(1) != 0:
        logger.info('Failed to stop MPRIS metadata process, killing')
        self.metadata_process.kill()
      self.metadata_process.communicate()

    self.metadata_process = None

    if self.mpris:
      logger.info('disconnecting proxy')
      disconnect_proxy(self.mpris)
    self.mpris = None
    logger.info("mpris closed")

    try:
      os.remove(self.metadata_path)
    except FileNotFoundError:
      pass
    except Exception as e:
      logger.exception(f'Could not remove metadata file: {e}')
    logger.info(f'Closed MPRIS {self.service_suffix}')

  def __del__(self):
    self.close()
