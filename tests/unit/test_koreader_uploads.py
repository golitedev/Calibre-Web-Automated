from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

pytestmark = pytest.mark.unit
PLUGIN = Path(__file__).resolve().parents[2] / "koreader/plugins/cwasync.koplugin"


@pytest.fixture
def lua():
    runtime = LuaRuntime(unpack_returned_tuples=True)
    runtime.execute("""
        store, tasks, requests = {}, {}, {}
        connected, success, hold = false, true, false
        clock, page, percent, position = 1700000000, 1, 0.1, 'xp-1'
        os.time = function() return clock end
        local serial = 0
        local noop = function() end
        G_reader_settings = {
            hasNot = function() return false end,
            readSetting = function(_, key, fallback) return store[key] or fallback end,
            saveSetting = function(_, key, value) store[key] = value end,
            flush = function() flushes = (flushes or 0) + 1 end,
        }
        store.cwasync = {server='https://cwa.example', username='reader', password='test-only', auto_sync=true}
        store.device_id = 'kobo'
        network = {isConnected=function() return connected end,
            goOnlineToRun=function(_, cb) connected=true; cb() end,
            willRerunWhenOnline=function() return false end}
        manager = {
            nextTick=function(_, fn) tasks[#tasks+1]=fn end,
            scheduleIn=function(_, delay, fn) tasks[#tasks+1]=fn end,
            unschedule=function(_, fn) for i=#tasks,1,-1 do if tasks[i]==fn then table.remove(tasks,i) end end end,
            getElapsedTimeSinceBoot=function() return 100 end,
            show=noop,
        }
        function runTasks()
            local current=tasks; tasks={}
            for _, fn in ipairs(current) do fn() end
        end
        local modules = {
            ['ui/widget/confirmbox']={}, ['device']={model='Kobo'},
            ['dispatcher']={registerAction=noop}, ['ui/event']={},
            ['ui/widget/infomessage']={new=function(_, t) return t end}, ['json']={},
            ['optmath']={roundPercent=function(n) return n end},
            ['ui/widget/multiinputdialog']={}, ['ui/network/manager']=network,
            ['ui/uimanager']=manager, ['logger']={dbg=noop,warn=noop},
            ['ffi/sha2']={}, ['random']={uuid=function() serial=serial+1; return tostring(serial) end},
            ['util']={}, ['bit']={}, ['ui/time']={s=function(n) return n end},
            ['ffi/util']={template=function(s) return s end},
            ['gettext']=function(s) return s end,
            ['ui/widget/container/widgetcontainer']={extend=function(_, t) return t end},
            ['CWAPrintPages']={new=function() return {setCount=noop,install=noop} end},
            ['CWASyncClient']={new=function(_, options)
                return {
                    update_progress=function(_, user, password, digest, progress, percentage, device, id, cb, timestamp)
                        requests[#requests+1]={url=options.service_url, document=digest, progress=progress,
                            percentage=percentage,timestamp=timestamp}
                        if hold then pending_callback=cb else cb(success,{}) end
                    end,
                    get_progress=function(_, user, password, digest, cb)
                        pulls=(pulls or 0)+1; cb(true,{print_page_count_managed=false})
                    end,
                }
            end},
        }
        for name, module in pairs(modules) do
            local value=module; package.preload[name]=function() return value end
        end
    """)
    runtime.globals().UploadQueue = runtime.execute((PLUGIN / "CWAUploadQueue.lua").read_text())
    runtime.execute("package.preload.CWAUploadQueue=function() return UploadQueue end")
    runtime.globals().Main = runtime.execute((PLUGIN / "main.lua").read_text())
    runtime.execute("""
        function makePlugin(reader)
            local ui = {menu={registerToMainMenu=function() end}}
            if reader then
                ui.document={info={has_pages=false},file='example.epub'}
                ui.doc_settings={readSetting=function(_, key)
                    if key=='partial_md5_checksum' then return string.rep('a',32) end
                end}
                ui.rolling={getLastPercent=function() return percent end,getLastProgress=function() return position end}
                ui.getCurrentPage=function() return page end
            end
            local plugin=setmetatable({ui=ui,view={document=ui.document},path='/plugin'}, {__index=Main})
            plugin:init()
            if reader then plugin:onReaderReady() end
            return plugin
        end
        plugin=makePlugin(true)
        runTasks()
    """)
    return runtime


def test_offline_position_survives_book_close_and_uploads_from_file_manager(lua):
    lua.execute("page=2;position='xp-2';percent=0.2; plugin:_onPageUpdate(page)")
    assert lua.eval("#requests") == 0
    assert lua.eval("flushes") > 0
    lua.execute("plugin:_onCloseDocument(); plugin:onCloseWidget(); plugin=makePlugin(false); connected=true; plugin:_onNetworkConnected(); runTasks()")
    assert lua.eval("#requests") == 1
    assert lua.eval("requests[1].url") == "https://cwa.example/kosync"
    assert lua.eval("requests[1].progress") == "xp-2"
    assert lua.eval("requests[1].timestamp") == 1700000000
    assert lua.eval("next(store.cwa_pending_uploads) == nil")


def test_reconnect_uploads_without_pulling_a_different_position(lua):
    lua.execute("page=2;position='xp-2';plugin:_onPageUpdate(page); connected=true;plugin:_onNetworkConnected();runTasks()")
    assert lua.eval("#requests") == 1
    assert lua.eval("position") == "xp-2"
    assert lua.eval("pulls") == 1  # metadata-only fetch, not a Goto action


def test_failed_upload_is_retained_and_retried_with_original_time(lua):
    lua.execute("page=2;plugin:_onPageUpdate(page); connected=true;success=false;plugin:flushPendingUploads(false)")
    assert lua.eval("next(store.cwa_pending_uploads) ~= nil")
    lua.execute("clock=clock+300;success=true;runTasks()")
    assert lua.eval("#requests") == 2
    assert lua.eval("requests[2].timestamp") == 1700000000
    assert lua.eval("next(store.cwa_pending_uploads) == nil")


def test_new_position_is_not_deleted_by_an_older_inflight_ack(lua):
    lua.execute("page=2;plugin:_onPageUpdate(page);connected=true;hold=true;plugin:flushPendingUploads(false)")
    lua.execute("clock=clock+10;page=3;position='xp-3';plugin:_onPageUpdate(page);pending_callback(true,{})")
    assert lua.eval("next(store.cwa_pending_uploads) ~= nil")
    # The final snapshot is drained automatically, even after closing the reader.
    lua.execute("plugin:onCloseWidget();pending_callback(true,{})")
    assert lua.eval("requests[2].progress") == "xp-3"
    assert lua.eval("next(store.cwa_pending_uploads) == nil")


def test_pending_days_keep_chronological_history_and_account_scope(lua):
    lua.execute("""
        plugin:_onPageUpdate(2)
        clock=clock+86400;position='xp-next-day';plugin:_onPageUpdate(3)
        store.cwasync.username='another-reader';connected=true;plugin:flushPendingUploads(false)
    """)
    assert lua.eval("#requests") == 0
    lua.execute("store.cwasync.username='reader';plugin:flushPendingUploads(false)")
    assert lua.eval("#requests") == 2
    assert lua.eval("requests[1].timestamp < requests[2].timestamp")


def test_manual_sync_works_even_when_automatic_sync_is_disabled(lua):
    lua.execute("store.cwasync.auto_sync=false;plugin:onCWASyncPushProgress()")
    assert lua.eval("#requests") == 1
    assert lua.eval("requests[1].progress") == "xp-1"


def test_opening_offline_book_does_not_connect_or_upload_unchanged_progress(lua):
    assert lua.eval("#requests") == 0
    assert lua.eval("pulls") is None
    assert lua.eval("connected") is False


def test_storage_failure_does_not_interrupt_reading(lua):
    lua.execute("G_reader_settings.flush=function() error('disk full') end;plugin:_onPageUpdate(2)")
    assert lua.eval("next(store.cwa_pending_uploads) ~= nil")


def test_first_time_configuration_is_saved_without_a_reading_upload(lua):
    lua.execute("""
        store.cwasync=nil
        fresh=makePlugin(false)
        fresh.settings.server='https://new-cwa.example'
        fresh.settings.username='new-reader'
        fresh:onSaveSettings()
    """)
    assert lua.eval("store.cwasync.username") == "new-reader"
    assert lua.eval("#requests") == 0


def test_manual_sync_does_not_claim_success_for_an_unidentified_book(lua):
    lua.execute("plugin.getDocumentDigest=function() return nil end;plugin:onCWASyncPushProgress()")
    assert lua.eval("#requests") == 0


def test_reconnection_does_not_resend_a_position_confirmed_by_manual_sync(lua):
    lua.execute("""
        page=2;plugin:_onPageUpdate(page)
        clock=clock+10;plugin:onCWASyncPushProgress()
        plugin:_onNetworkConnected();runTasks()
    """)
    assert lua.eval("#requests") == 1


def test_account_change_during_upload_does_not_send_other_books_under_new_credentials(lua):
    lua.execute("""
        plugin:_onPageUpdate(2)
        clock=clock+86400;position='next-day';plugin:_onPageUpdate(3)
        connected=true;hold=true;plugin:flushPendingUploads(false)
        store.cwasync.username='another-reader';hold=false;pending_callback(true,{})
    """)
    assert lua.eval("#requests") == 1
    assert lua.eval("next(store.cwa_pending_uploads) ~= nil")
