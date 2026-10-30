-- Cloned from https://github.com/koreader/koreader/tree/master/plugins/cwasync.koplugin

local _ = require("gettext")
return {
    name = "cwasync",
    fullname = _("CWA Progress Sync"),
    description = _([[Synchronizes reading progress with Calibre Web Automated and automatically applies CWA print page counts to the footer and reading statistics.]]),
}
