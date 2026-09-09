"""Regression runner: model calls must be mocked; only local HTTP test servers are allowed."""
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib import request
from urllib.parse import urlsplit

original_open=request.OpenerDirector.open

def local_only(opener, req, *args, **kwargs):
    url=req.full_url if isinstance(req,request.Request) else req
    if urlsplit(url).hostname not in ('127.0.0.1','localhost','::1'):
        raise AssertionError('Regression tests must mock external API calls')
    return original_open(opener,req,*args,**kwargs)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.discover(str(Path(__file__).parent),pattern='test_*.py')
    with patch.object(request.OpenerDirector,'open',local_only):
        result=unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
