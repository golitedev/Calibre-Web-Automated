from pathlib import Path

import pytest
from lupa.luajit21 import LuaRuntime

pytestmark = pytest.mark.unit
PLUGIN = Path(__file__).resolve().parents[2] / "koreader/plugins/cwasync.koplugin"


@pytest.fixture
def lua():
    runtime = LuaRuntime(unpack_returned_tuples=True)
    runtime.globals().PrintPages = runtime.execute((PLUGIN / "CWAPrintPages.lua").read_text())
    runtime.execute("""
        native_total, native_page = 814, 407
        cached = {}
        document = {
            info = {has_pages = false},
            getPageCount = function() return native_total end,
            hasHiddenFlows = function() return false end,
        }
        ui = {
            document = document,
            getCurrentPage = function() return native_page end,
            doc_settings = {
                saveSetting = function(_, key, value) cached[key] = value end,
                delSetting = function(_, key) cached[key] = nil end,
            },
        }
        stats = {
            ui = ui, document = document, data = {pages = 814}, id_curr_book = 1,
            insertDB = function(s, target)
                last_raw_total, last_target = s.data.pages, target or s.data.pages
            end,
            onPageUpdate = function(s, page) recorded_page = page end,
            getCurrentStat = function(s)
                s:insertDB()
                s.data.pages = s.document:getPageCount()
                return s.ui:getCurrentPage(), s.data.pages
            end,
            getCurrentBookReadPages = function() return {[190] = {1, 0}} end,
        }
        footer = {
            ui = ui, pageno = 407, pages = 814,
            genFooterText = function(f) return f.pageno .. '/' .. f.pages end,
            updateFooterText = function(f) text = f:genFooterText() end,
            maybeUpdateFooter = function() end,
        }
        ui.statistics, ui.view = stats, {footer = footer}
        adapter = PrintPages.new(ui)
        adapter:setCount(380)
        adapter:install()
    """)
    return runtime


def test_footer_uses_custom_count_without_changing_engine_or_sync_progress(lua):
    lua.execute("footer:updateFooterText()")
    assert lua.globals().text == "190/380"
    assert lua.eval("document:getPageCount()") == 814
    assert lua.eval("ui:getCurrentPage()") == 407
    assert lua.eval("footer.pageno") == 407
    assert lua.eval("footer.pages") == 814


def test_statistics_scale_target_preserves_native_records_and_positions(lua):
    lua.execute("stats:onPageUpdate(408)")
    assert lua.globals().recorded_page == 408
    assert lua.globals().last_raw_total == 814
    assert lua.globals().last_target == 380
    assert lua.eval("stats:getCurrentStat()") == (190, 380)
    assert lua.eval("stats.data.pages") == 814
    assert lua.eval("stats.document == document")


def test_font_changes_keep_the_same_custom_total(lua):
    lua.execute("native_total = 1628; native_page = 814; footer.pageno = 814; stats.data.pages = 1628; footer:updateFooterText(); stats:insertDB(1628)")
    assert lua.globals().text == "190/380"
    assert lua.globals().last_raw_total == 1628
    assert lua.globals().last_target == 380


def test_clearing_reverts_footer_and_statistics_without_erasing_raw_history(lua):
    lua.execute("adapter:setCount(nil); footer:updateFooterText(); stats:insertDB()")
    assert lua.globals().text == "407/814"
    assert lua.globals().last_target == 814
    assert lua.eval("cached.cwa_print_page_count") is None


def test_cached_count_is_available_offline_and_install_is_idempotent(lua):
    lua.execute("offline = PrintPages.new(ui); offline:setCount(cached.cwa_print_page_count)")
    assert lua.eval("offline.count") == 380
    lua.execute("original_insert = stats.insertDB; adapter:install()")
    assert lua.eval("original_insert == stats.insertDB")


def test_book_map_still_receives_native_screen_coordinates(lua):
    lua.execute("mapped = stats:getCurrentBookReadPages()")
    assert lua.eval("mapped[405] ~= nil")
    assert lua.eval("mapped[407] ~= nil")
    assert lua.eval("mapped[190] == nil")


def test_presentation_failure_restores_shared_state(lua):
    lua.execute("""
        original_generate = function() error('other plugin failed') end
        footer.genFooterText = original_generate
        success = pcall(footer.updateFooterText, footer)
    """)
    assert lua.globals().success is False
    assert lua.eval("footer.ui == ui and footer.pageno == 407 and footer.pages == 814")
    assert lua.eval("footer.genFooterText == original_generate")


def test_invalid_counts_do_not_replace_valid_cached_count(lua):
    lua.execute("adapter:setCount(0); adapter:setCount(380.5); adapter:setCount(0/0)")
    assert lua.eval("adapter.count") == 380
    assert lua.eval("cached.cwa_print_page_count") == 380


def test_fixed_layout_documents_keep_native_numbering(lua):
    lua.execute("document.info.has_pages = true; footer:updateFooterText(); stats:insertDB()")
    assert lua.globals().text == "407/814"
    assert lua.globals().last_target == 814


def test_plugin_sources_parse_with_luajit(lua):
    for path in PLUGIN.glob("*.lua"):
        lua.eval("function(source) assert(loadstring(source)) end")(path.read_text())


def test_main_plugin_applies_metadata_without_needing_progress_or_moving_position(lua):
    lua.execute("""
        local names = {
            'ui/widget/confirmbox', 'device', 'dispatcher', 'ui/event',
            'ui/widget/infomessage', 'json', 'optmath', 'ui/widget/multiinputdialog',
            'ui/network/manager', 'ui/uimanager', 'logger', 'ffi/sha2',
            'random', 'util', 'bit',
        }
        for _, name in ipairs(names) do package.preload[name] = function() return {} end end
        package.preload['ui/time'] = function() return {s = function(n) return n end} end
        package.preload['ffi/util'] = function() return {template = function(s) return s end} end
        package.preload['gettext'] = function() return function(s) return s end end
        package.preload['ui/widget/container/widgetcontainer'] = function()
            return {extend = function(_, t) return t end}
        end
        G_reader_settings = {hasNot = function() return false end}
    """)
    lua.globals().Main = lua.execute((PLUGIN / "main.lua").read_text())
    lua.execute("""
        plugin = {print_pages = adapter, ui = ui}
        -- No percentage or position is present: a newly downloaded book.
        Main.applyPrintPages(plugin, {print_page_count_managed = true, print_page_count = 400})
    """)
    assert lua.eval("adapter.count") == 400
    assert lua.eval("ui:getCurrentPage()") == 407
    lua.execute("Main.applyPrintPages(plugin, {})")
    assert lua.eval("adapter.count") == 400  # older servers preserve cached values
    lua.execute("""
        Main.applyPrintPages(plugin, {print_page_count_managed=true, print_page_count=380,
            print_page_count_observed_at='2026-10-30T04:30:01.000000+00:00'})
        Main.applyPrintPages(plugin, {print_page_count_managed=true, print_page_count=400,
            print_page_count_observed_at='2026-10-30T04:30:00.000000+00:00'})
    """)
    assert lua.eval("adapter.count") == 380  # older same-book snapshot is ignored
    lua.execute("Main.applyPrintPages(plugin, {print_page_count_managed = true})")
    assert lua.eval("adapter.count") is None  # null-dropping JSON decoders clear too
    lua.execute("Main.applyPrintPages(plugin, {print_page_count_managed = true, print_page_count = 380}, {})")
    assert lua.eval("adapter.count") is None  # response belongs to a previous book
    lua.execute("""
        plugin._cwa_closed = true
        Main.applyPrintPages(plugin, {print_page_count_managed = true, print_page_count = 380}, document)
    """)
    assert lua.eval("adapter.count") is None  # document has already closed
