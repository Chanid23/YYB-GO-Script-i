"""Offline regression tests; optional --har PATH uses user's HAR only in memory."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

spec = importlib.util.spec_from_file_location("tbsg", Path(__file__).resolve().parents[1] / "wx-script" / "elm_cookie.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)
HAR = None
if "--har" in sys.argv:
    i = sys.argv.index("--har")
    HAR = sys.argv[i + 1]
    del sys.argv[i:i + 2]


def add_cookies(client, values, url=tb.LOGIN_URL):
    headers = Message()
    for value in values:
        headers.add_header("Set-Cookie", value)
    class Response:
        def info(self):
            return headers
    client.jar.extract_cookies(Response(), Request(url))


def login_payload():
    return {"hasError": False, "content": {"success": True, "data": {
        "resultCode": 100, "loginResult": "success", "USERID": "123", "unb": "456"}}}


def session():
    c = tb.Client()
    add_cookies(c, [f'{k}={v}; Domain=.ele.me; Path=/' for k, v in {
        "SID": '"fake=="', "USERID": "123", "UTUSER": "123", "cookie2": "c2", "unb": "456"}.items()])
    return c


class Tests(unittest.TestCase):
    def test_multiple_services_not_routed_to_first_server(self):
        values = tb.load_accounts("localhost:8000@one\nhttps://other.example@two\nlocalhost:8000@one")
        self.assertEqual([(x['server'], x['ref']) for x in values],
                         [('http://localhost:8000', 'one'), ('https://other.example', 'two')])

    def test_bad_configuration_rejected(self):
        for v in ('', 'https://a.example@', 'https://user:pass@a.example@x'):
            with self.subTest(value=v), self.assertRaises(tb.FlowError):
                tb.load_accounts(v)

    def test_bare_address_enumerates_accounts(self):
        class Fake:
            def request(self, *args):
                return {'code': 0, 'data': {'list': [{'id': 7}, {'openid': 'two'}]}}
        values = tb.load_accounts('https://yyb.example', client_factory=Fake)
        self.assertEqual([x['ref'] for x in values], ['7', 'two'])

    def test_explicit_account_server_override(self):
        values = tb.load_accounts('https://first.example', '[{"ref":"a","server":"https://second.example"}]')
        self.assertEqual(values[0]['server'], 'https://second.example')

    def test_code_nested_and_outer_failure(self):
        class Fake:
            def request(self, *args, **kwargs):
                self.body = args[2]
                return {'code': 0, 'data': {'result': '{"wxCode":"fresh_code_12345"}'}}
        f = Fake()
        self.assertEqual(tb.get_code({'server': 'https://a.example', 'ref': 'one'}, f), 'fresh_code_12345')
        self.assertEqual(f.body['app_id'], tb.APP_ID)
        f.request = lambda *a, **k: {'code': 409, 'message': 'secret-do-not-log', 'data': {'code': 'fresh_code_12345'}}
        with self.assertRaises(tb.FlowError) as error:
            tb.get_code({'server': 'https://a.example', 'ref': 'one'}, f)
        self.assertNotIn('secret-do-not-log', str(error.exception))

    def test_duplicate_cookies_quoting_and_wrong_domain(self):
        c = session()
        add_cookies(c, ['cookie2=new; Domain=.ele.me; Path=/',
                        'SID=foreign; Domain=.eleme.cn; Path=/',
                        'XSRF-TOKEN=private; Path=/'])
        self.assertEqual(c.cookies()['cookie2'], 'new')
        self.assertEqual(c.cookies()['SID'], '"fake=="')
        self.assertNotIn('XSRF-TOKEN', c.cookies())
        self.assertNotIn('foreign', c.cookie_text())

    def test_login_success_requires_business_result_and_cookie_identity(self):
        p, c = login_payload(), session()
        tb.validate_login(p, c)
        p['content']['data']['USERID'] = 'other'
        with self.assertRaises(tb.FlowError):
            tb.validate_login(p, c)

    def test_http200_cannot_prove_login(self):
        for p in ({'content': {'success': False}}, {'content': {'success': True, 'data': {}}},
                  {'hasError': True, 'content': {'success': True}}):
            with self.subTest(payload=p), self.assertRaises(tb.FlowError):
                tb.validate_login(p, session())

    def test_resultcode100_redirect_is_manual_login_not_cookie_success(self):
        for key,url,expected in (
            ('redirectUrl','https://ipassport.ele.me/mini_login.htm?token=private-token','后续登录'),
            ('iframeRedirectUrl','https://ipassport.ele.me/iv/remote/h5/login_check.htm?havana_iv_token=private-token','安全校验')):
            payload={'hasError':False,'content':{'success':True,'data':{'resultCode':100,key:url}}}
            with self.subTest(key=key),self.assertRaises(tb.FlowError) as error:
                tb.validate_login(payload,tb.Client())
            self.assertIn(expected,str(error.exception))
            self.assertNotIn('private-token',str(error.exception))

    def test_body_cookie_values_without_set_cookie_are_not_exported(self):
        with self.assertRaises(tb.FlowError):
            tb.validate_login(login_payload(), tb.Client())

    def test_token_retry_recomputes_signature(self):
        c = session()
        seen = []
        def request(method, url, data, headers):
            q = parse_qs(urlsplit(url).query)
            seen.append(q['sign'][0])
            if len(seen) == 1:
                add_cookies(c, ['_m_h5_tk=newtoken_9999999999999; Domain=.ele.me; Path=/'])
                return {'ret': ['FAIL_SYS_TOKEN_EMPTY::token needed']}
            self.assertEqual(q['sign'][0], tb.mtop_sign('newtoken', q['t'][0], data['data']))
            return {'ret': ['SUCCESS::ok'], 'data': {}}
        c.request = request
        tb.mtop(c, tb.CHECK_API, {})
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])

    def test_waf_not_retried_and_no_raw_message(self):
        c = session()
        c.request = lambda *a: {'ret': ['RGV587_ERROR::secret-login-token']}
        with self.assertRaises(tb.FlowError) as error:
            tb.mtop(c, tb.CHECK_API, {})
        self.assertIn('RGV587_ERROR', str(error.exception))
        self.assertNotIn('secret-login-token', str(error.exception))

    def test_complete_chain_uses_fresh_code_separate_clients(self):
        calls, instances = [], []
        class Fake(tb.Client):
            def __init__(self):
                super().__init__()
                instances.append(self)
            def request(self, method, url, data=None, headers=None, json_body=False):
                calls.append((urlsplit(url).path, data))
                if url.endswith('/wxapp/getCode'):
                    return {'code': 0, 'data': {'result': {'code': 'fresh_code_12345'}}}
                if url == tb.LOGIN_URL:
                    self.assert_code = json.loads(data['authorizationCode'])
                    self.assert_umid = data.get('umidToken')
                    self.assert_bx = headers.get('bx-ua')
                    add_cookies(self, [f'{k}={v}; Domain=.ele.me; Path=/' for k,v in
                                      {'SID':'s','USERID':'123','UTUSER':'123','cookie2':'c2','unb':'456'}.items()])
                    return login_payload()
                return {'ret': ['SUCCESS::ok'], 'data': {}}
        with patch.object(tb,'build_login_context',return_value=({'bx-ua':'synthetic-bx'},'synthetic-umid')), \
             patch.object(tb,'_warm_guide') as existing_guide:
            result = tb.exchange({'server':'https://yyb.example','ref':'one'}, client_factory=Fake)
            existing_guide.assert_called_once_with(instances[1])
        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[1].assert_code, {'authorizationCode':'fresh_code_12345'})
        self.assertEqual(instances[1].assert_umid,'synthetic-umid')
        self.assertEqual(instances[1].assert_bx,'synthetic-bx')
        self.assertIn('USERID=123', result)
        self.assertEqual([path for path, data in calls], [
            '/wxapp/getCode', '/mini_program/login.do', f'/h5/{tb.CHECK_API}/1.0/5.0/'])

    def test_login_params_are_generated_before_one_time_code(self):
        events=[];c=session();c.request=lambda *a:login_payload()
        def context():events.append('params');return {},''
        def code(*a):events.append('code');return 'fresh_code_12345'
        with patch.object(tb,'build_login_context',side_effect=context),patch.object(tb,'get_code',side_effect=code), \
             patch.object(tb,'mtop',return_value={'ret':['SUCCESS::ok']}),patch.object(tb,'_warm_guide'):
            tb.exchange({'server':'https://yyb.example','ref':'one'},client_factory=lambda:c)
        self.assertEqual(events,['params','code'])

    def test_success_body_supplements_missing_identity_preserving_header_values(self):
        c=tb.Client();add_cookies(c,['sgcookie=server%2Bvalue; Domain=.ele.me; Path=/'])
        p=login_payload();p['content']['data'].update({'SID':'opaque==','UTUSER':'123','sid':'c2','sgcookie':'body+value'})
        tb.absorb_login_cookie(c,p);tb.validate_login(p,c)
        self.assertEqual(c.cookies()['cookie2'],'c2')
        self.assertEqual(c.cookies()['sgcookie'],'server%2Bvalue')
        self.assertEqual(c.cookies()['SID'],'opaque==')

    def test_body_cookie_encoding_and_userid_aliases(self):
        c=tb.Client();p=login_payload();d=p['content']['data'];d.pop('USERID');d.pop('unb')
        d.update({'userId':'123','munb':'456','SID':'opaque==','cookie2':'c2','sgcookie':'a+b/c='})
        tb.absorb_login_cookie(c,p);tb.validate_login(p,c)
        self.assertEqual(c.cookies()['USERID'],'123')
        self.assertEqual(c.cookies()['unb'],'456')
        self.assertEqual(c.cookies()['sgcookie'],'a%2Bb%2Fc%3D')

    def test_partial_redirect_body_never_becomes_identity_cookie(self):
        c=tb.Client();p={'content':{'success':True,'data':{'resultCode':100,'cookie2':'partial',
             'redirectUrl':'https://ipassport.ele.me/mini_login.htm?token=synthetic'}}}
        tb.absorb_login_cookie(c,p)
        self.assertEqual(c.cookies(),{})
        with self.assertRaises(tb.FlowError):tb.validate_login(p,c)

    def test_body_identity_conflict_is_not_overwritten_or_accepted(self):
        c=session();p=login_payload();p['content']['data']['USERID']='other'
        tb.absorb_login_cookie(c,p)
        self.assertEqual(c.cookies()['USERID'],'123')
        with self.assertRaises(tb.FlowError):tb.validate_login(p,c)

    def test_awsc_bridge_returns_required_headers_without_secret_logging(self):
        def run(args,**kwargs):
            Path(args[-1]).write_text(json.dumps({'ok':True,'headers':{'bx-ua':'synthetic-bx',
                'mini-janus':'synthetic-janus','bx-umidtoken':'synthetic-umid','Authorization':'should-be-dropped'}}))
            return type('Result',(),{'returncode':0})()
        log=io.StringIO()
        with patch.object(tb.subprocess,'run',side_effect=run),patch.object(tb.shutil,'which',return_value='node'), \
             patch.dict(os.environ,{'elm_awsc_enabled':'1'},clear=True),contextlib.redirect_stdout(log):
            headers,umid=tb.build_login_context()
        self.assertEqual(umid,'synthetic-umid');self.assertNotIn('Authorization',headers)
        self.assertNotIn('synthetic-bx',log.getvalue())

    def test_awsc_failure_keeps_strict_base_login_fallback(self):
        with patch.object(tb.subprocess,'run',side_effect=tb.subprocess.TimeoutExpired('node',35)), \
             patch.object(tb.shutil,'which',return_value='node'),patch.dict(os.environ,{'elm_awsc_enabled':'1'},clear=True), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tb.build_login_context(),({},''))

    def test_failure_preserves_old_file_and_redacts_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'ELEME_COOKIE.txt'
            output.write_text('old', encoding='utf-8')
            log = io.StringIO()
            with patch.dict(os.environ, {'YYB_SERVER':'https://yyb.example@one'}), \
                 patch.object(tb, 'select_writer', return_value=(object(), '本地青龙')), \
                 patch.object(tb, 'exchange', side_effect=RuntimeError('private-token')), \
                 contextlib.redirect_stdout(log):
                self.assertEqual(tb.main(['--output',str(output)]), 1)
            self.assertEqual(output.read_text(), 'old')
            self.assertNotIn('private-token', log.getvalue())

    def test_ql_preserves_remark_updates_only_identity_and_reads_back(self):
        q = object.__new__(tb.QingLong)
        c = 'SID=s; USERID=123; unb=456'
        requests = []
        def request(method, path, data=None):
            requests.append((method, data))
            if method == 'PUT': return {'code':200}
            return {'code':200,'data':[{'id':1,'name':'ELEME_COOKIE','value':c,'remarks':'custom'},
                                       {'id':2,'name':'ELEME_COOKIE','value':'USERID=999','remarks':'other'}]}
        q.request = request
        q.sync(c)
        self.assertEqual(requests[1][1]['id'],1)
        self.assertEqual(requests[1][1]['remarks'],'custom')
        self.assertEqual([x[0] for x in requests], ['GET','PUT','GET'])

    def test_ql_rejects_multi_account_variable(self):
        q = object.__new__(tb.QingLong)
        q.request = lambda *a: {'data':[{'name':'ELEME_COOKIE','value':'USERID=1\nUSERID=123'}]}
        with self.assertRaises(tb.FlowError):
            q.sync('USERID=123')

    def test_target_defaults_local_and_full_remote_config_switches(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(tb,'LocalQingLong') as local, patch.object(tb,'QingLong') as remote:
            writer, mode = tb.select_writer()
            self.assertEqual(mode, '本地青龙')
            self.assertIs(writer, local.return_value)
            remote.assert_not_called()
        with patch.dict(os.environ, {'elm_ql_url':'https://ql.example','elm_ql_client_id':'id','elm_ql_client_secret':'secret'}, clear=True), \
             patch.object(tb,'LocalQingLong') as local, patch.object(tb,'QingLong') as remote:
            writer, mode = tb.select_writer()
            self.assertEqual(mode, '远程青龙')
            self.assertIs(writer, remote.return_value)
            local.assert_not_called()

    def test_partial_remote_config_never_falls_back_to_local(self):
        for config in ({'elm_ql_url':'https://ql.example'}, {'elm_ql_client_id':'id','elm_ql_client_secret':'secret'}):
            with patch.dict(os.environ, config, clear=True), patch.object(tb,'LocalQingLong') as local:
                with self.assertRaises(tb.FlowError): tb.select_writer()
                local.assert_not_called()

    def test_generic_ql_preload_credentials_do_not_trigger_remote_mode(self):
        with patch.dict(os.environ, {'QL_URL':'https://ql.example','QL_CLIENT_ID':'id','QL_CLIENT_SECRET':'secret'}, clear=True), \
             patch.object(tb,'LocalQingLong') as local, patch.object(tb,'QingLong') as remote:
            writer,mode=tb.select_writer()
            self.assertEqual(mode,'本地青龙')
            remote.assert_not_called()

    def test_independent_config_area_switches_remote_without_environment(self):
        with patch.dict(os.environ,{},clear=True), \
             patch.object(tb,'CONFIG_ELM_QL_URL','https://configured.example'), \
             patch.object(tb,'CONFIG_ELM_QL_CLIENT_ID','configured-id'), \
             patch.object(tb,'CONFIG_ELM_QL_CLIENT_SECRET','configured-secret'), \
             patch.object(tb,'QingLong') as remote,patch.object(tb,'LocalQingLong') as local:
            writer,mode=tb.select_writer()
            self.assertEqual(mode,'远程青龙')
            self.assertEqual(tb.remote_config(),('https://configured.example','configured-id','configured-secret'))
            local.assert_not_called()

    def test_environment_overrides_independent_config_and_empty_uses_default(self):
        with patch.object(tb,'CONFIG_ELM_QL_URL','https://configured.example'), \
             patch.object(tb,'CONFIG_ELM_QL_CLIENT_ID','configured-id'), \
             patch.object(tb,'CONFIG_ELM_QL_CLIENT_SECRET','configured-secret'), \
             patch.dict(os.environ,{'elm_ql_url':'https://env.example','elm_ql_client_id':'','elm_ql_client_secret':'env-secret'},clear=True):
            self.assertEqual(tb.remote_config(),('https://env.example','configured-id','env-secret'))

    def test_partial_independent_config_does_not_write_local(self):
        with patch.dict(os.environ,{},clear=True),patch.object(tb,'CONFIG_ELM_QL_URL','https://configured.example'), \
             patch.object(tb,'LocalQingLong') as local:
            with self.assertRaises(tb.FlowError):tb.select_writer()
            local.assert_not_called()

    def test_local_insert_update_preserves_other_accounts_and_disabled_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tb,'CONFIG_ELM_COOKIE_ENV_NAME','CUSTOM_COOKIE'):
            db = Path(directory)/'database.sqlite'
            conn=sqlite3.connect(db)
            conn.execute('CREATE TABLE Envs (id INTEGER PRIMARY KEY,value TEXT,name TEXT,remarks TEXT,status INTEGER,position INTEGER,timestamp TEXT,isPinned INTEGER,labels TEXT,createdAt TEXT NOT NULL,updatedAt TEXT NOT NULL)')
            conn.execute("INSERT INTO Envs (name,value,remarks,status,position,createdAt,updatedAt) VALUES ('ELEME_COOKIE','USERID=999','other',0,1,'n','n')")
            conn.commit();conn.close()
            writer=object.__new__(tb.LocalQingLong);writer.db=db
            calls=[]
            def request(method,path,data=None):
                calls.append(method)
                if method=='PUT':return {'code':200}
                with writer.connect() as c:
                    return {'code':200,'data':[dict(x) for x in c.execute('SELECT * FROM Envs')]}
            writer.request=request
            writer.sync('SID=a; USERID=123; unb=456')
            with writer.connect() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM Envs').fetchone()[0],2)
                c.execute("UPDATE Envs SET remarks='custom',status=1 WHERE value LIKE '%USERID=123%'")
                c.commit()
            writer.sync('SID=b; USERID=123; unb=456')
            with writer.connect() as c:
                rows=c.execute('SELECT * FROM Envs ORDER BY id').fetchall()
                self.assertEqual(rows[0]['value'],'USERID=999')
                self.assertEqual(rows[1]['value'],'SID=b; USERID=123; unb=456')
                self.assertEqual(rows[1]['name'],'CUSTOM_COOKIE')
                self.assertEqual(rows[1]['remarks'],'custom')
                self.assertEqual(rows[1]['status'],1)
            self.assertEqual(calls,['PUT','GET','PUT','GET'])

    def test_remote_cookie_variable_name_defaults_and_configuration_override(self):
        for config,env,expected in (
            ('ELEME_COOKIE',{},'ELEME_COOKIE'),
            ('CONFIG_COOKIE',{},'CONFIG_COOKIE'),
            ('CONFIG_COOKIE',{'elm_cookie_ENV_NAME':'ENV_COOKIE'},'ENV_COOKIE')):
            with self.subTest(expected=expected),patch.object(tb,'CONFIG_ELM_COOKIE_ENV_NAME',config),patch.dict(os.environ,env,clear=True):
                q=object.__new__(tb.QingLong);names=[];cookie='USERID=123'
                def request(method,path,data=None):
                    if method=='POST':
                        names.append(data[0]['name']);return {'code':200}
                    return {'code':200,'data':[] if not names else [{'name':names[0],'value':cookie}]}
                q.request=request;q.sync(cookie)
                self.assertEqual(names,[expected])

    def test_invalid_cookie_environment_name_is_rejected_before_target_setup(self):
        for name in ('bad name','9COOKIE','`ELEME_COOKIE`'):
            with self.subTest(name=name),patch.dict(os.environ,{'elm_cookie_ENV_NAME':name},clear=True), \
                 patch.object(tb,'LocalQingLong') as local,patch.object(tb,'QingLong') as remote:
                with self.assertRaises(tb.FlowError):tb.select_writer()
                local.assert_not_called();remote.assert_not_called()

    def test_local_auth_recovers_from_expired_internal_token_using_existing_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'db').mkdir();(root/'config').mkdir()
            db=root/'db/database.sqlite'
            conn=sqlite3.connect(db)
            conn.execute('CREATE TABLE Envs (id INTEGER PRIMARY KEY,value TEXT,name TEXT,remarks TEXT,status INTEGER,position INTEGER,timestamp TEXT,isPinned INTEGER,labels TEXT,createdAt TEXT,updatedAt TEXT)')
            conn.execute('CREATE TABLE Apps (client_id TEXT,client_secret TEXT,scopes TEXT)')
            conn.execute('INSERT INTO Apps VALUES (?,?,?)',('test-app','test-secret','["envs"]'))
            conn.commit();conn.close()
            (root/'config/token.json').write_text('{"value":"expired-token"}',encoding='utf-8')
            requests=[]
            class FakeClient:
                def request(self,method,url,data=None,headers=None,json_body=False):
                    requests.append(urlsplit(url))
                    if '/open/auth/token?' in url:
                        return {'code':200,'data':{'token':'new-app-token'}}
                    if headers['Authorization']=='Bearer expired-token':
                        raise tb.FlowError('HTTP 401')
                    return {'code':200,'data':[]}
            with patch.dict(os.environ,{'QL_DB_PATH':str(db),'QL_URL':'https://external.example'},clear=True), \
                 patch.object(tb,'Client',FakeClient):
                writer=tb.LocalQingLong()
                self.assertEqual(writer.token,'new-app-token')
            self.assertTrue(all(p.hostname=='127.0.0.1' for p in requests))

    @unittest.skipUnless(HAR, 'Provide --har PATH to validate actual capture')
    def test_actual_har_login_cookie_parser(self):
        h = json.loads(Path(HAR).read_text(encoding='utf-8-sig'))
        found = 0
        for e in h['log']['entries']:
            if urlsplit(e['request']['url']).path != '/mini_program/login.do':
                continue
            form = parse_qs(e['request']['postData']['text'], keep_blank_values=True)
            self.assertEqual(form['appId'][0], tb.APP_ID)
            self.assertEqual(set(form), set(tb.LOGIN_FIELDS) | {'authorizationCode'})
            raw = e['response']['content']['text']
            if e['response']['content'].get('encoding') == 'base64':
                raw = base64.b64decode(raw).decode('utf-8','replace')
            p = json.loads(raw)
            c = tb.Client()
            add_cookies(c, [x['value'] for x in e['response']['headers'] if x['name'].lower()=='set-cookie'])
            # Boolean assertions keep real credentials out of failure diagnostics.
            try:
                tb.validate_login(p, c)
                passed = True
            except tb.FlowError:
                passed = False
            self.assertTrue(passed, 'Captured login did not pass business/cookie checks')
            self.assertTrue(bool(c.cookies().get('sgcookie')))
            self.assertTrue(c.cookies().get('SID','').strip('"') == str(p['content']['data']['SID']).strip('"'))
            found += 1
        self.assertEqual(found, 2)


if __name__ == '__main__':
    unittest.main()
