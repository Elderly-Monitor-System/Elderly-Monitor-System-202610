"""照片接口测试；隔离 SQLite 与 Flask 客户端"""
import base64
import concurrent.futures
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app
import photos

# 测试 JPEG：1×1
JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a'
    'HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy'
    'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEB'
    'AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQID'
    'AAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RF'
    'RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKz'
    'tLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEB'
    'AQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMi'
    'MoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVm'
    'Z2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU'
    '1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5/ooooA//2Q=='
)


def headers(seq=1, reason='FALL', event=7, device='esp32-test', boot='boot1'):
    return {'Content-Type': 'image/jpeg', 'X-Device-Id': device, 'X-Boot-Id': boot,
            'X-Photo-Seq': str(seq), 'X-Photo-Reason': reason, 'X-Event-Seq': str(event),
            'X-Trigger-Up-Ms': '300000', 'X-Capture-Up-Ms': '300150'}


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = dict(app.config)
        app.config.update(TESTING=True, PHOTO_DB_PATH=str(Path(self.temp.name) / 'photos.sqlite3'), PHOTO_MAX_BYTES=512 * 1024)
        self.db_guard = patch('app.db_mod.get_pool', side_effect=AssertionError('Photo endpoints must not touch MySQL'))
        self.db_guard.start()
        self.client = app.test_client()

    def tearDown(self):
        self.db_guard.stop()
        app.config.clear()
        app.config.update(self.previous)
        self.temp.cleanup()

    def upload(self, **kw):
        return self.client.post('/api/photos', data=JPEG, headers=headers(**kw))

    def test_persistent_upload_original_image_and_duplicate(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.json)
        photo = response.json['photo']
        self.assertEqual((photo['width'], photo['height']), (1, 1))
        self.assertTrue(photo['received_at'].endswith('+08:00'))
        image = self.client.get(photo['url'])
        self.assertEqual(image.data, JPEG)
        self.assertEqual(image.mimetype, 'image/jpeg')
        cached = self.client.get(photo['url'], headers={'If-None-Match': image.headers['ETag']})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(len(app.test_client().get('/api/photos').json['photos']), 1)
        with closing(sqlite3.connect(app.config['PHOTO_DB_PATH'])) as db:
            self.assertEqual(db.execute('SELECT jpeg FROM photos').fetchone()[0], JPEG)

    def test_metadata_conflict_does_not_overwrite(self):
        self.upload()
        self.assertEqual(self.upload(reason='AIR').status_code, 409)
        self.assertEqual(self.client.get('/api/photos').json['photos'][0]['reason'], 'FALL')

    def test_all_allowed_reasons_and_tilt_rejected(self):
        for i, reason in enumerate(('TIMER', 'FALL', 'AIR', 'MANUAL'), 1):
            self.assertEqual(self.upload(seq=i, reason=reason, event=i if reason in {'FALL', 'AIR'} else 0).status_code, 201)
        self.assertEqual(self.upload(seq=5, reason='TILT').status_code, 400)
        self.assertEqual(self.upload(seq=6, event=0).status_code, 400)
        self.assertEqual(self.upload(seq=7, reason='TIMER', event=7).status_code, 400)

    def test_cursor_pagination_and_exact_alarm_correlation(self):
        for seq in range(1, 6):
            self.upload(seq=seq)
        self.upload(seq=1, boot='boot2')
        first = self.client.get('/api/photos?limit=2').json
        self.assertEqual([x['photo_seq'] for x in first['photos']], [1, 5])
        next_page = self.client.get('/api/photos?limit=2&before=' + str(first['next_before'])).json
        self.assertEqual([x['photo_seq'] for x in next_page['photos']], [4, 3])
        selected = self.client.get('/api/photos?device_id=esp32-test&boot_id=boot2&event_seq=7&reason=FALL').json
        self.assertEqual(len(selected['photos']), 1)
        self.assertEqual(selected['photos'][0]['boot_id'], 'boot2')
        self.assertEqual(self.client.get('/api/photos?reason=AIR').json['photos'], [])

    def test_validation_and_limits(self):
        self.assertEqual(self.client.post('/api/photos', data=JPEG).status_code, 415)
        self.assertEqual(self.upload(device='../escape').status_code, 400)
        for bad in (b'', b'not jpeg', JPEG[:-20], b'\xff\xd8broken\xff\xd9'):
            self.assertEqual(self.client.post('/api/photos', data=bad, headers=headers()).status_code, 400)
        self.assertEqual(self.client.post('/api/photos', data=b'x' * (512 * 1024 + 1), headers=headers()).status_code, 413)
        for query in ('reason=TILT', 'event_seq=-1', 'device_id=../x', 'before=0'):
            self.assertEqual(self.client.get('/api/photos?' + query).status_code, 400)
        self.assertEqual(self.client.get('/api/photos/999/image').status_code, 404)

    def test_concurrent_retry_is_idempotent(self):
        self.client.get('/api/photos')
        def send(_):
            return app.test_client().post('/api/photos', data=JPEG, headers=headers()).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            codes = list(executor.map(send, range(8)))
        self.assertEqual(codes.count(201), 1)
        self.assertEqual(codes.count(200), 7)
        self.assertEqual(len(self.client.get('/api/photos').json['photos']), 1)

    def test_pages_and_assets_render_without_mysql(self):
        for url in ('/photos', '/static/photos.js', '/static/photos.css'):
            with self.client.get(url) as response:
                self.assertEqual(response.status_code, 200)
        with app.app_context():
            from flask import render_template
            html = render_template('index.html')
            self.assertIn('查看现场照片', html)
            self.assertIn('photoPreview', html)

    def test_original_ingest_protocol_still_uses_mysql(self):
        self.db_guard.stop()
        with patch('app.db_mod.get_pool') as pool, patch('app.ingest.handle', return_value=True) as handle:
            response = self.client.post('/api/ingest', json={'t': 'event', 'etype': 'TILT'})
            self.assertEqual(response.status_code, 200)
            pool.assert_called_once()
            self.assertEqual(handle.call_args.args[1]['etype'], 'TILT')
        self.db_guard.start()


if __name__ == '__main__':
    unittest.main(verbosity=2)
