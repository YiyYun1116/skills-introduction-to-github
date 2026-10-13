"""Single-owner website access; deploy behind an HTTPS reverse proxy."""
import base64
import binascii
import hmac
import os

from fastapi.responses import Response


def validate_access_config():
    username = os.environ.get('SIGNAL_ATLAS_ACCESS_USER','research')
    password = os.environ.get('SIGNAL_ATLAS_ACCESS_PASSWORD','')
    production = os.environ.get('SIGNAL_ATLAS_ENV','local') == 'production'
    if not username or ':' in username or len(username)>80:
        raise ValueError('SIGNAL_ATLAS_ACCESS_USER 不能为空、含冒号或超过80个字符')
    if production and len(password)<16:
        raise ValueError('生产部署需要在托管平台设置至少16个字符的 SIGNAL_ATLAS_ACCESS_PASSWORD')
    return production


def check_access(request):
    # Keep only the narrow health endpoint public for the hosting platform.
    if request.url.path == '/api/health' and request.method == 'GET':
        return None
    expected_password=os.environ.get('SIGNAL_ATLAS_ACCESS_PASSWORD','')
    if not expected_password:
        if os.environ.get('SIGNAL_ATLAS_ENV','local')=='production':
            return Response('访问保护尚未配置。',status_code=503)
        return None
    authorization=request.headers.get('authorization','')
    authenticated=False
    if len(authorization)<=4096:
        scheme,_,token=authorization.partition(' ')
        if scheme.lower()=='basic':
            try:
                decoded=base64.b64decode(token,validate=True).decode('utf-8')
                username,separator,password=decoded.partition(':')
                valid_user=hmac.compare_digest(username.encode(),os.environ.get('SIGNAL_ATLAS_ACCESS_USER','research').encode())
                valid_password=hmac.compare_digest(password.encode(),expected_password.encode())
                authenticated=bool(separator) and valid_user and valid_password
            except (binascii.Error,UnicodeDecodeError,ValueError):
                pass
    if authenticated:
        return None
    return Response('此工作台需要访问账号和密码。请使用部署时设置的凭据。',status_code=401,
                    headers={'WWW-Authenticate':'Basic realm="Signal Atlas", charset="UTF-8"'})
