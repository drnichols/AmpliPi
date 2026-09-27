import pytest
import context

def test_validation_switched_off():
  # AirPlay names can only be 50chars long; test that this obj can be created without validation
  # enabled, and that it does do validation normally
  kwargs = {
    'name': 'a'*51,
    'mock': True,
    'ap2': False,
  }
  assert context.amplipi.streams.AirPlay(validate=False, **kwargs)

  with pytest.raises(context.amplipi.streams.base_streams.InvalidStreamField):
    context.amplipi.streams.AirPlay(**kwargs)


@pytest.mark.parametrize('server, port, url', [
  (None, None, None),
  ('ma.local', None, 'ws://ma.local:8927/sendspin'),
  ('192.168.1.10', 9000, 'ws://192.168.1.10:9000/sendspin'),
  ('ws://ma.local:8927/sendspin', 1234, 'ws://ma.local:8927/sendspin'),
])
def test_sendspin_server_url(server, port, url):
  # a bare host gets expanded to the default sendspin websocket url, full urls are passed through untouched
  stream = context.amplipi.streams.Sendspin('Kitchen', server, port, mock=True)
  assert stream.server_url() == url
