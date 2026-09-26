from typing import Optional, Dict
from celery import Celery
import requests

app = Celery('amplipi', broker='redis://localhost:6379')


@app.task
def post(url: str, data: Optional[Dict] = None, timeout: int = 5) -> None:
  """ Send a POST request to the specified URL optionally with the provided data. """
  print(f'Posting {data} to {url}')
  response = requests.post(url, json=data, timeout=timeout)
  if response.ok:
    # go-librespot answers most player commands with 204 No Content, so only print a body when there is one
    if response.content:
      print(response.json())
  else:
    print(f'Error posting to {url}: {response.status_code}')


if __name__ == '__main__':
  app.start()
