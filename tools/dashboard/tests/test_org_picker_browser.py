"""L2.B shared organization control: actual browser, Alpine, and page markup."""
import subprocess
import pytest
from tools.dashboard.test_lib.l2b_harness import (
    start_mock_server, stop_mock_server, open_browser, close_browser,
    _ab_eval_batch,
    _navigate_and_check,
)


@pytest.fixture(scope="module")
def picker_browser(tmp_path_factory):
    server = start_mock_server({}, tmp_path_factory.mktemp("org-picker"), port=0)
    open_browser(server["url"] + "/sessions")
    yield server
    close_browser()
    stop_mock_server(server)


class TestOrgPickerBehavior:
    @pytest.mark.parametrize("width,height", [(390, 844), (1440, 1000)])
    def test_icon_keyboard_reactive_update_and_viewport(self, picker_browser, width, height):
        subprocess.run(["agent-browser", "set", "viewport", str(width), str(height)], check=True, capture_output=True)
        result = _ab_eval_batch(r"""
          return (async () => {
            const host = document.createElement('div');
            host.style.cssText='position:fixed;right:8px;top:80px;z-index:10001;width:160px';
            host.setAttribute('x-data', `{value:'a', orgs:[{slug:'a',name:'A very long organization name',favicon:'/static/icon-192.png'},{slug:'b',name:'Broken',favicon:'/static/not-an-icon.png',color:'#123456'},{slug:'c',name:'Fallback',color:'#456789'}]}`);
            host.innerHTML=`<div x-org-picker="{orgs, value, onChange: slug => value=slug}"></div><button data-after>After</button>`;
            document.body.append(host);
            await Alpine.nextTick();
            const data=Alpine.$data(host), trigger=host.querySelector('.org-picker-trigger');
            trigger.click();
            const menu=document.getElementById(trigger.getAttribute('aria-controls'));
            await Promise.all(Array.from(document.querySelectorAll('.org-picker-image')).map(img=>img.complete ? Promise.resolve() : new Promise(r=>{img.addEventListener('load',r,{once:true});img.addEventListener('error',r,{once:true});})));
            const r={};
            r.real_icon=!!trigger.querySelector('img:not([hidden])')?.naturalWidth;
            const broken=menu.querySelector('[data-slug=b]');
            r.fallback=broken.textContent.includes('B') && !broken.querySelector('img');
            r.open=trigger.getAttribute('aria-expanded')==='true' && !menu.hidden;
            const rect=menu.getBoundingClientRect();
            r.bounds=rect.left>=0 && rect.right<=innerWidth && rect.top>=0 && rect.bottom<=innerHeight;
            const first=menu.querySelector('[data-slug=a]'); first.focus();
            data.orgs=data.orgs.map(o=>({...o})); await Alpine.nextTick();
            r.stable=trigger===host.querySelector('.org-picker-trigger') && document.activeElement===first && !menu.hidden;
            const key=k=>document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:k,bubbles:true,cancelable:true}));
            key('End'); key('Enter'); await Alpine.nextTick();
            r.selected=data.value==='c' && menu.hidden && trigger.textContent.includes('Fallback');
            trigger.click(); key('Escape');
            r.escape=menu.hidden && document.activeElement===trigger;
            trigger.click(); document.body.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
            r.outside=menu.hidden;
            data.orgs=[]; await Alpine.nextTick(); r.empty=trigger.disabled;
            host.remove(); await Alpine.nextTick(); r.cleanup=!menu.isConnected;
            return r;
          })();
        """)
        assert result and all(result.values()), result

    @pytest.mark.parametrize("route,root,inventory,value,callback,testid", [
        ("sessions", "sessionsPage()", "orgFilterList", "selectedOrg", "pickOrgFilter", "org-filter-toggle"),
        ("search", "searchPage()", "orgList", "selectedOrg", "pickOrg", "sp-org-chip"),
        ("worktrees", "worktreesPage()", "orgs", "selectedOrg", "setOrg", "worktrees-org-select"),
        ("beads", "beadsPage()", "orgs", "selectedOrg", "setOrg", "beads-org-select"),
    ])
    def test_page_consumes_shared_control(self, picker_browser, route, root, inventory, value, callback, testid):
        subprocess.run(["agent-browser", "set", "viewport", "390", "844"], check=True, capture_output=True)
        _navigate_and_check('/' + route + ('?q=picker' if route == 'search' else ''), '')
        # Page state injection is the established L2.B seam. Keep the actual
        # template/directive and exercise its callback without invoking writes.
        result = _ab_eval_batch(f"""
          return (async () => {{
            let el;
            for(let n=0;n<50;n++) {{
              el=Array.from(document.querySelectorAll('[x-data]')).find(e=>e.getAttribute('x-data')==={root!r});
              if(el && el._x_dataStack) break;
              await new Promise(r=>setTimeout(r,40));
            }}
            if (!el) return {{root:false,location:location.pathname}};
            const data=Alpine.$data(el);
            if ({route!r} === 'search') data.query='picker';
            data[{inventory!r}]=[{{slug:'a',name:'Alpha',favicon:'/static/icon-192.png',worktrees:1,commits:2}},{{slug:'b',name:'Beta',worktrees:0,commits:0}}];
            data[{value!r}]='a'; let chosen=null;
            data[{callback!r}]=slug=>{{chosen=slug;data[{value!r}]=slug;}};
            await Alpine.nextTick();
            const trigger=document.querySelector('[data-testid="{testid}"]');
            if (!trigger) return {{trigger:false}};
            const triggerTop=trigger.getBoundingClientRect().top;
            const mobileRowOk={route!r}!=='search' || innerWidth>430 ||
              Array.from(document.querySelector('.sp-filter-row-1').children).every(e=>Math.abs(e.getBoundingClientRect().top-triggerTop)<8);
            trigger.click();
            const menu=document.getElementById(trigger.getAttribute('aria-controls'));
            if (!menu) return {{shared:false}};
            menu.querySelector('[data-slug=b]').click(); await Alpine.nextTick();
            return {{visible:trigger.getBoundingClientRect().width>0,selected:chosen==='b',closed:menu.hidden,mobileRowOk}};
          }})();
        """)
        assert result and all(result.values()), result

    def test_tab_moves_to_adjacent_page_control(self, picker_browser):
        result = _ab_eval_batch(r"""
          const host=document.createElement('div');host.id='tab-picker-proof';
          host.style.cssText='position:fixed;top:100px;left:10px;z-index:10001';
          host.innerHTML='<button id="picker-before">Before</button><div id="tab-picker"></div><button id="picker-after">After</button>';
          document.body.append(host);
          window.tabPicker=OrgPicker.mount(host.querySelector('#tab-picker'),{orgs:[{slug:'a',name:'Alpha'}],value:'a'});
          host.querySelector('.org-picker-trigger').click();
          return document.activeElement.getAttribute('role')==='option';
        """)
        assert result
        subprocess.run(["agent-browser", "press", "Tab"],check=True,capture_output=True)
        assert _ab_eval_batch("return document.activeElement.id==='picker-after';")
        _ab_eval_batch("document.querySelector('#tab-picker .org-picker-trigger').click();")
        subprocess.run(["agent-browser", "press", "Shift+Tab"],check=True,capture_output=True)
        assert _ab_eval_batch("return document.activeElement.id==='picker-before';")
        _ab_eval_batch("window.tabPicker.destroy();document.querySelector('#tab-picker-proof').remove();")
