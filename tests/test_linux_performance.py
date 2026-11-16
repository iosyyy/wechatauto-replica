"""性能路径保持实时账号证据和最终提交前的完整界面校验。"""
from dataclasses import replace
import unittest

import test_linux_native as native_tests
import test_linux_sender as sender_tests
from test_linux_sender import CoordinateCLI, node, snapshot
from wechatauto.platforms.linux_sender import (
    AgentDesktopDriver, LinuxSenderError, Selector, Target,
)


class SourceGuardTests(unittest.TestCase):
    setUp = native_tests.NativeTests.setUp
    tearDown = native_tests.NativeTests.tearDown
    rewrite = native_tests.NativeTests.rewrite
    start = native_tests.NativeTests.start
    guard = native_tests.GuardTests.guard

    def test_configured_confirmation_budget_only_changes_readback_deadline(self):
        self.config['delivery_confirm_timeout'] = 23
        backend = self.start()
        sender = backend._get_sender()
        self.assertEqual(sender.verify_timeout, 23)

    def test_source_probe_keeps_current_fd_hmac_and_window_without_full_tree(self):
        guard = self.guard()
        self.assertTrue(hasattr(guard, 'verify_source'))
        self.assertTrue(guard.verify_source('wxid_fixture'))
        self.assertFalse(any(args[0] == 'snapshot' for args in self.probes))
        self.assertTrue(any(args[0] == 'list-windows' for args in self.probes))
        fd = self.proc / '42/fd/3'
        fd.unlink()
        self.assertFalse(guard.verify_source('wxid_fixture'))

    def test_history_only_group_is_filtered_in_sql_without_enabling_send(self):
        chat = native_tests.CHAT
        self.config['native_history'] = {'token':'h'*40,
            'owners':[{'bot_uuid':'fixture-bot','launcher_id':'fixture-owner'}], 'allowed_groups':[chat]}
        backend = self.start()
        self.assertEqual(backend.list_groups(limit=1,allowed_groups=[chat])[0]['username'],chat)
        self.assertEqual(backend.list_groups(limit=1,allowed_groups=['missing@chatroom']),[])
        self.assertEqual(backend.list_groups(limit=1,offset=1,allowed_groups=[chat]),[])
        self.assertEqual(backend.get_history(chat,limit=1)[0]['content'],'hello')
        with self.assertRaises(PermissionError):backend.send_text(chat,'must not send',[],'denied')

    def test_group_prefix_routes_requests_but_keeps_ordinary_messages_in_local_history(self):
        import hashlib
        chat = native_tests.CHAT
        self.config.update(allowed_chats=['filehelper',chat],group_request_prefix='/lb ')
        backend = self.start();backend.poll_once()
        table='Msg_'+hashlib.md5(chat.encode()).hexdigest()
        self.rewrite('message/message_0.db', "INSERT INTO %s VALUES(2,1,1,1700000000,'ordinary discussion',NULL,NULL,NULL,102,2);"
                     "INSERT INTO %s VALUES(3,1,1,1700000000,'/lb summary please',NULL,NULL,NULL,103,3);" % (table,table))
        self.assertEqual(backend.poll_once(),1)
        self.assertEqual(self.journal.unacked()[0]['payload']['content']['str'],'wxid_alice:\n/lb summary please')
        self.assertIn('ordinary discussion',[x['content'] for x in backend.get_history(chat,limit=10)])

    def test_source_probe_cannot_rebind_restarted_or_other_account_process(self):
        guard = self.guard()
        self.assertTrue(hasattr(guard, 'verify_source'))
        self.assertTrue(guard.verify_source('wxid_fixture'))
        self.assertFalse(guard.verify_source('wxid_other'))
        self.process_stat.write_text('42 (wechat) ' + ' '.join(['S'] + ['1'] * 18 + ['456']))
        self.assertFalse(guard.verify_source('wxid_fixture'))


class FastDriverTests(unittest.TestCase):
    def driver(self, cli):
        layout = replace(sender_tests.CoordinateRouteTests().layout(), activation_mode='focused_return',
                         composer_input_mode='set_value', reuse_verified_controls=True,
                         chat_row=Selector('option', name_first_line='{header_title}'))
        run = cli.__call__
        def invoke(args, stdin=None):
            if args[0] == 'focus':
                cli.calls.append((args, stdin)); cli.focused = 'send'
                return {'ok': True, 'data': {}}
            if args[0] == 'is':
                cli.calls.append((args, stdin))
                return {'ok': True, 'data': {'property': 'focused', 'result': cli.focused == 'send'}}
            if args[:2] == ['press', 'return']:
                cli.calls.append((args, stdin)); cli.sent += 1; cli.composer = ''
                return {'ok': True, 'data': {}}
            return run(args, stdin)
        return AgentDesktopDriver(layout, run=invoke, allow_physical=True)

    def test_only_preparation_and_final_pre_submit_need_full_trees(self):
        cli = CoordinateCLI(); cli.chat_open = True
        driver = self.driver(cli); target = Target('wxid_friend', 'Fixture Friend')
        driver.prepare_text(target, '中文\nbody')
        driver.submit_text(target, '中文\nbody')
        self.assertEqual(cli.sent, 1)
        self.assertEqual(sum(a[0] == 'snapshot' for a, _ in cli.calls), 2)
        self.assertEqual(sum(a[0] == 'set-value' for a, _ in cli.calls), 1)

    def test_login_is_refused_before_draft_write(self):
        cli = CoordinateCLI(); cli.chat_open = True
        old = cli.state
        def login(window):
            result = old(window)
            result['tree']['children'].append(node('button', ' Log In ', '@m:login'))
            return result
        cli.state = login
        driver = self.driver(cli)
        with self.assertRaises(LinuxSenderError):
            driver.prepare_text(Target('wxid_friend', 'Fixture Friend'), 'must not write')
        self.assertEqual(cli.composer, '')
        self.assertFalse(any(a[0] == 'set-value' for a, _ in cli.calls))

    def test_visible_unique_chat_row_avoids_ambiguous_global_history_search(self):
        cli = CoordinateCLI()
        driver = self.driver(cli)
        driver.prepare_text(Target('wxid_friend','Fixture Friend'), 'body')
        self.assertEqual(cli.composer, 'body')
        self.assertFalse(any(a[0] == 'clipboard-set' for a, _ in cli.calls))
        self.assertEqual([a[a.index('--xy')+1] for a, _ in cli.calls if a[0]=='mouse-click'], ['110,240'])

    def test_final_view_is_fresh_after_slow_guard_and_blocks_wrong_chat(self):
        cli = CoordinateCLI(); cli.chat_open = True
        driver = self.driver(cli); target = Target('wxid_friend', 'Fixture Friend')
        driver.prepare_text(target, 'body')
        with self.assertRaises(LinuxSenderError):
            driver.submit_text(target, 'body', before_submit=lambda: setattr(cli, 'chat_open', False))
        self.assertEqual(cli.sent, 0)

    def test_stale_preparation_cannot_submit_another_body_or_target(self):
        cli = CoordinateCLI(); cli.chat_open = True
        driver = self.driver(cli); target = Target('wxid_friend', 'Fixture Friend')
        driver.prepare_text(target, 'first')
        for other, body in [(target, 'other'), (Target('wxid_other', target.title), 'first')]:
            with self.subTest(body=body), self.assertRaises(LinuxSenderError):
                driver.submit_text(other, body)
        self.assertEqual(cli.sent, 0)

    def test_dynamic_composer_uses_verified_system_display_alias(self):
        base = sender_tests.CoordinateRouteTests().layout()
        driver = AgentDesktopDriver(replace(base, header_aliases={'filehelper':'File Transfer'},
                                            composer=Selector('textfield', name='{title}')),
                                    run=lambda *a, **kw: None, allow_physical=True)
        tree = snapshot(node('label','File Transfer','@s:header'),
                        node('textfield','File Transfer','@s:composer'),
                        node('button','Fixture Send','@s:send'))['tree']
        composer, _ = driver._chat_controls(tree, Target('filehelper','文件传输助手'))
        self.assertEqual(composer['ref_id'], '@s:composer')
        self.assertEqual(driver._chat_ready_selector(Target('filehelper','文件传输助手')), 'textfield:File Transfer')
