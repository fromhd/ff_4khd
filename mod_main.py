import json
import traceback
import urllib.parse
import requests
import os
import threading
from flask import Response, jsonify, render_template, stream_with_context, request
from plugin import PluginModuleBase, default_route_socketio_module
from .logic_4khd import Logic4KHD

class ModuleMain(PluginModuleBase):
    template_prefix = "ff_4khd"

    def __init__(self, P):
        super(ModuleMain, self).__init__(P, name="main")
        self.db_default = {
            '4khd_url': 'https://4khd.com',
            'proxy_enabled': 'True',
            'download_path': os.path.join(os.getcwd(), 'downloads', 'ff_4khd'),
        }
        self.menu = {
            'main': '최신',
            'popular': '인기',
            'cosplay': '코스플레이',
            'album': '앨범',
            'search': '검색',
            'setting': '설정',
            'log': '로그'
        }
        self.download_status = {} # {url: {current: 0, total: 0, status: 'ready'}}
        default_route_socketio_module(self)

    def process_menu(self, sub, req):
        arg = self.P.ModelSetting.to_dict()
        arg['package_name'] = self.P.package_name
        
        # 메뉴 설정
        menu_map = {
            'main': {'title': '최신', 'category': ''},
            'popular': {'title': '인기', 'category': 'popular'},
            'cosplay': {'title': '코스플레이', 'category': 'cosplay'},
            'album': {'title': '앨범', 'category': 'album'},
            'search': {'title': '검색'},
            'setting': {'title': '설정'},
            'log': {'title': '로그'}
        }
        
        if sub in ['main', 'popular', 'cosplay', 'album']:
            arg['title'] = menu_map[sub]['title']
            arg['category'] = menu_map[sub]['category']
            return render_template(f"{self.P.package_name}_main.html", arg=arg)
        elif sub == 'search':
            arg['title'] = '검색'
            return render_template(f"{self.P.package_name}_search.html", arg=arg)
        elif sub == 'setting':
            arg['title'] = '설정'
            return render_template(f"{self.template_prefix}_setting.html", arg=arg)
        elif sub == 'log':
            arg['title'] = '로그'
            return render_template("log.html", package=self.P.package_name)
        return render_template("sample.html", title=f"4KHD - {sub}")

    def process_ajax(self, sub, req):
        try:
            if sub == 'setting_save':
                ret = self.P.ModelSetting.setting_save(req)
                return jsonify(ret)

            if sub == 'list':
                page = int(req.form.get('page', 1))
                search = req.form.get('search', '')
                category = req.form.get('category', '')
                base_url = self.P.ModelSetting.get('4khd_url')
                data = Logic4KHD.get_list(base_url=base_url, page=page, search=search, category=category)
                if self.P.ModelSetting.get_bool('proxy_enabled'):
                    for item in data:
                        if item.get('thumbnail'):
                            item['thumbnail'] = self._make_proxy_url(item['thumbnail'])
                return jsonify({'ret': 'success', 'data': data})
            
            if sub == 'detail':
                url = req.form.get('url')
                data = Logic4KHD.get_detail(url)
                if data:
                    if self.P.ModelSetting.get_bool('proxy_enabled'):
                        # 원본 URL 보존을 위해 별도 리스트 생성 후 프록시 적용
                        data['proxy_images'] = [self._make_proxy_url(img) for img in data['images']]
                    return jsonify({'ret': 'success', 'data': data})
                return jsonify({'ret': 'error', 'log': '상세 정보를 가져올 수 없습니다.'})

            if sub == 'download':
                url = req.form.get('url')
                force = req.form.get('force') == 'true'
                target_path = self.P.ModelSetting.get('download_path')
                
                # 중복 다운로드 확인
                if not force:
                    data = Logic4KHD.get_detail(url)
                    if data:
                        safe_title = "".join([c for c in data['title'] if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
                        zip_file_path = os.path.join(target_path, f"{safe_title}.zip")
                        if os.path.exists(zip_file_path):
                            return jsonify({'ret': 'exists', 'msg': '이미 다운로드된 파일이 있습니다. 다시 받으시겠습니까?'})

                if not os.path.exists(target_path):
                    os.makedirs(target_path)
                
                # 상태 초기화 및 시작
                self.download_status[url] = {'current': 0, 'total': 0, 'status': 'starting'}
                threading.Thread(target=self.download_thread, args=(url, target_path), daemon=True).start()
                return jsonify({'ret': 'success', 'msg': '다운로드를 시작합니다. 버튼에서 진행률을 확인하세요.'})

            if sub == 'download_status':
                url = req.form.get('url')
                status = self.download_status.get(url, {'status': 'none'})
                return jsonify(status)
                
        except Exception as e:
            self.P.logger.error(f"Exception:{str(e)}")
            self.P.logger.error(traceback.format_exc())
            return jsonify({'ret': 'error', 'log': str(e)})

    def download_thread(self, url, target_path):
        """백그라운드 이미지 다운로드 (JPG 변환 + ZIP 압축 + 진행률 기록)"""
        try:
            import zipfile
            import shutil
            from io import BytesIO
            try:
                from PIL import Image
            except: Image = None

            self.P.logger.info(f'[4KHD] Download Task Started: {url}')
            data = Logic4KHD.get_detail(url)
            if not data:
                self.download_status[url] = {'status': 'error', 'msg': '상세 정보 로딩 실패'}
                return

            # 안전한 파일명/폴더명 생성
            safe_title = "".join([c for c in data['title'] if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
            temp_folder = os.path.join(target_path, f"temp_{safe_title}")
            zip_file_path = os.path.join(target_path, f"{safe_title}.zip")

            if not os.path.exists(temp_folder):
                os.makedirs(temp_folder)

            session = Logic4KHD.get_session()
            total = len(data['images'])
            self.download_status[url] = {'current': 0, 'total': total, 'status': 'downloading'}
            
            downloaded_files = []

            for idx, img_url in enumerate(data['images']):
                try:
                    res = session.get(img_url, headers=Logic4KHD.HEADERS, timeout=30)
                    if res.status_code == 200:
                        file_path = os.path.join(temp_folder, f"{idx+1:03d}.jpg")
                        if Image and not img_url.lower().endswith('.gif'):
                            img = Image.open(BytesIO(res.content))
                            if img.mode != 'RGB': img = img.convert('RGB')
                            img.save(file_path, 'JPEG', quality=95, subsampling=0)
                        else:
                            with open(file_path, 'wb') as f: f.write(res.content)
                        downloaded_files.append(file_path)
                    
                    # 진행률 업데이트
                    self.download_status[url]['current'] = idx + 1
                except: pass

            # ZIP 압축 시작
            if downloaded_files:
                self.download_status[url]['status'] = 'zipping'
                with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for f in downloaded_files:
                        zipf.write(f, os.path.basename(f))
                
                shutil.rmtree(temp_folder)
                self.download_status[url]['status'] = 'completed'
                self.P.logger.info(f'[4KHD] Download & Zip Completed: {zip_file_path}')
            else:
                self.download_status[url]['status'] = 'error'
                if os.path.exists(temp_folder): shutil.rmtree(temp_folder)

        except Exception as e:
            self.download_status[url] = {'status': 'error', 'msg': str(e)}
            self.P.logger.error(f'[4KHD] Download thread failed: {e}')
            self.P.logger.error(traceback.format_exc())

    def process_normal(self, sub, req):
        if sub == 'proxy':
            return self._proxy(req)
        return "Not Found", 404

    def _make_proxy_url(self, target):
        return f"/{self.P.package_name}/normal/proxy?url={urllib.parse.quote(target)}"

    def _proxy(self, req):
        target_url = req.args.get('url')
        if not target_url: return "No URL", 400
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': Logic4KHD.BASE_URL}
        
        try:
            session = Logic4KHD.get_session()
            res = None
            try:
                res = session.get(target_url, headers=headers, stream=True, timeout=10)
            except:
                try:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    res = session.get(target_url, headers=headers, stream=True, timeout=10, verify=False)
                except Exception as e:
                    return str(e), 500
            
            if not res or res.status_code >= 400:
                return "Target Error", res.status_code if res else 500
            
            def generate():
                try:
                    for chunk in res.iter_content(chunk_size=1024*64):
                        yield chunk
                finally:
                    res.close()
            
            response = Response(stream_with_context(generate()), status=res.status_code, content_type=res.headers.get('Content-Type'))
            response.headers['Cache-Control'] = 'public, max-age=604800'
            return response
        except Exception as e:
            return str(e), 500

    def plugin_load(self):
        pass

    def plugin_unload(self):
        pass
