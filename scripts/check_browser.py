"""Meaningful UI checks on a loopback preview plus a fully offline copy."""
import csv
import functools
import io
import json
import re
import shutil
import sqlite3
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


def main():
    checks=[];errors=[]
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(root)))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    db=sqlite3.connect(root/'data/processed/corpus.sqlite')
    expected=db.execute("SELECT count(*) FROM tokens WHERE plain='الله'").fetchone()[0]
    expected2=db.execute("SELECT count(*) FROM tokens WHERE plain='الله' AND surah_id=2 AND is_opening_basmala=0").fetchone()[0]
    db.close()
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=shutil.which('chromium') or None,headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1050},accept_downloads=True)
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'http://127.0.0.1:{server.server_port}/dashboard/index.html',wait_until='load')
        assert page.locator('#scope').input_value()=='file'
        page.screenshot(path=str(root/'report/browser_overview.png'))
        for tab in ['overview','findings','realworld','dictionary','morphology','surahs','repeats','numerical','methods']:
            page.locator(f'[data-tab="{tab}"]').click();assert page.locator('#'+tab).is_visible()
        assert page.locator('#realworld-list .world-case').count()==10
        assert page.locator('#realworld-findings .finding').count()==46
        assert '309,21' in page.locator('#realworld-list').inner_text() or '309.21' in page.locator('#realworld-list').inner_text()
        page.locator('[data-tab="realworld"]').click()
        page.get_by_text('Все 46 находок обычным языком').click()
        page.locator('#world-finding-query').fill('J-day-365')
        assert page.locator('#realworld-findings .finding').count()==1
        page.locator('#world-finding-query').fill('')
        page.screenshot(path=str(root/'report/browser_real_world.png'))
        checks.append({'check':'navigation','status':'pass','tabs':9})
        no_js=browser.new_context(java_script_enabled=False)
        static_page=no_js.new_page()
        static_page.goto(f'http://127.0.0.1:{server.server_port}/dashboard/index.html',wait_until='load')
        static_page.locator('[data-tab="findings"]').click()
        assert static_page.locator('#findings').is_visible()
        assert not static_page.locator('#overview').is_visible()
        static_page.locator('[data-tab="realworld"]').click()
        assert static_page.locator('#realworld').is_visible()
        checks.append({'check':'native_tab_navigation_without_javascript','status':'pass'})
        no_js.close()
        page.locator('[data-tab="dictionary"]').click();page.fill('#word-query','الله')
        page.wait_for_function("state.query===normalizeQuery('الله')")
        button=page.locator('#word-list').get_by_role('button',name='الله',exact=True)
        row=button.locator('xpath=ancestor::tr');count=int(re.sub(r'\D','',row.locator('td').nth(1).inner_text()))
        assert count==expected;button.click()
        assert page.locator('#occurrences .occurrence').count()==12
        assert page.locator('#occurrences .occurrence').first.locator('.address strong').inner_text()=='1:1:2'
        first=page.locator('#occurrences .occurrence').first.inner_text()
        page.locator('#occ-pages-top').get_by_role('button',name='Следующая страница').click()
        assert page.locator('#occurrences .occurrence').first.inner_text()!=first
        with page.expect_download() as dl:
            page.click('#download-occurrences')
        rows=list(csv.DictReader(io.StringIO(Path(dl.value.path()).read_text(encoding='utf-8-sig'))))
        assert len(rows)==expected and rows[0]['token_id']=='1:1:2'
        checks.append({'check':'dictionary_and_complete_download','status':'pass','form':'الله','scope':'file','expected_source_count':expected,'download_rows':len(rows)})
        page.select_option('#scope','numbered');page.select_option('#surah','2')
        button=page.locator('#word-list').get_by_role('button',name='الله',exact=True)
        assert int(re.sub(r'\D','',button.locator('xpath=ancestor::tr').locator('td').nth(1).inner_text()))==expected2
        button.click();assert page.locator('#occurrences .occurrence').first.locator('.address strong').inner_text().startswith('2:')
        page.locator('#word-detail').screenshot(path=str(root/'report/browser_arabic_contexts.png'))
        checks.append({'check':'surah_and_scope_filters','status':'pass','surah':2,'scope':'numbered','count':expected2})
        page.select_option('#surah','all')
        for unit,term in [('lemma','الله'),('root','قول')]:
            page.select_option('#unit',unit);page.fill('#word-query',term)
            page.wait_for_function('(q)=>state.query===normalizeQuery(q)',arg=term)
            assert page.locator('#word-list .term-button').count()>0
            page.locator('#word-list .term-button').first.click()
            assert page.locator('#occurrences .occurrence').count()>0
        checks.append({'check':'lemma_root_search','status':'pass','note_ru':'Поиск без огласовок находит внешние нормализованные единицы; контексты исходного файла доступны.'})
        page.locator('[data-tab="findings"]').click();page.select_option('#finding-family','J')
        # All five published claims, including insufficiently specified outcomes.
        assert '365' in page.locator('#findings-list').inner_text()
        assert 'недостаточно специфицировано' in page.locator('#findings-list').inner_text()
        checks.append({'check':'hypothesis_filters','status':'pass'})
        for tab in ['overview','surahs','numerical']:
            page.locator(f'[data-tab="{tab}"]').click()
            for img in page.locator(f'#{tab} img').all():
                img.scroll_into_view_if_needed()
                img.evaluate('(e)=>e.decode()')
        imgs=page.locator('img').evaluate_all('(els)=>els.map(e=>({src:e.src,ok:e.complete&&e.naturalWidth>0}))')
        assert all(x['ok'] for x in imgs)
        checks.append({'check':'embedded_static_figures','status':'pass','images':len(imgs)})
        broken=[]
        from urllib.parse import unquote,urlparse
        for href in page.locator('a[href]').evaluate_all('(els)=>els.map(e=>e.getAttribute("href"))'):
            if href.startswith(('../','./')):
                path=(root/'dashboard'/unquote(urlparse(href).path)).resolve()
                if not path.exists():broken.append(href)
        assert not broken,broken
        checks.append({'check':'local_links','status':'pass'})
        mobile=browser.new_context(viewport={'width':390,'height':844},is_mobile=True)
        mp=mobile.new_page();mp.goto(page.url.split('#')[0],wait_until='load')
        assert mp.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
        mp.screenshot(path=str(root/'report/browser_mobile.png'))
        mp.locator('[data-tab="realworld"]').click()
        assert mp.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
        mp.locator('#realworld .section-head').scroll_into_view_if_needed()
        mp.screenshot(path=str(root/'report/browser_real_world_mobile.png'))
        checks.append({'check':'mobile_layout','status':'pass','viewport':'390×844'})
        offline=browser.new_context(offline=True,viewport={'width':1280,'height':900})
        op=offline.new_page();op.on('pageerror',lambda e:errors.append(str(e)))
        op.set_content((root/'dashboard/index.html').read_text(encoding='utf-8'),wait_until='load')
        op.locator('[data-tab="dictionary"]').click();op.fill('#word-query','الله')
        op.wait_for_function("state.query===normalizeQuery('الله')")
        assert op.locator('#word-list').get_by_role('button',name='الله',exact=True).count()==1
        checks.append({'check':'offline_embedded_application','status':'pass','note_ru':'HTML загружен как сохранённый документ в контекст с отключённой сетью; исходный file:// ограничен политикой браузера среды.'})
        assert not errors,errors
        browser.close()
    server.shutdown()
    result={'status':'pass','checks':checks,'javascript_errors':errors,'limitations_ru':'Проверены локальный HTTP и документ без сети. file:// в управляемом Chromium блокируется административной политикой; политика не менялась. XLSX проверен структурно через openpyxl, визуальный просмотр в настольном Excel не выполнялся.'}
    (root/'results/browser_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
