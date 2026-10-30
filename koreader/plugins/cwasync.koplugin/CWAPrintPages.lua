-- Estimated print pages supplied by CWA. Navigation and KOSync continue to
-- use the original document positions; only presentation and stats scale.
local PrintPages = {}
PrintPages.__index = PrintPages
local unpack = table.unpack or unpack

local function pack(...)
    return { n = select("#", ...), ... }
end

-- Delegate methods with their original receiver (important for engine userdata).
local function proxy(original, overrides)
    return setmetatable(overrides, { __index = function(_, key)
        local value = original[key]
        if type(value) == "function" then
            return function(_, ...) return value(original, ...) end
        end
        return value
    end })
end

function PrintPages.new(ui)
    return setmetatable({ ui = ui }, PrintPages)
end

function PrintPages:enabled()
    return self.count and not self.ui.document.info.has_pages
end

function PrintPages:page(native_page)
    local native_total = self.ui.document:getPageCount()
    return math.max(1, math.min(self.count, math.ceil(native_page * self.count / native_total)))
end

function PrintPages:setCount(value)
    if type(value) ~= "number" then value = nil end
    if value and (value ~= value or value % 1 ~= 0 or value < 1 or value > 100000) then return end
    if value == self.count then return end
    local stats = self.ui.statistics
    if stats and stats.id_curr_book then stats:insertDB() end
    self.count = value
    if value then
        self.ui.doc_settings:saveSetting("cwa_print_page_count", value)
    else
        self.ui.doc_settings:delSetting("cwa_print_page_count")
    end
    if not self.ready then return end
    self:install()
    -- Update the book's target scale. Raw page_stat_data remains untouched.
    if stats and stats.id_curr_book then stats:insertDB() end
    if self.ui.view and self.ui.view.footer then self.ui.view.footer:maybeUpdateFooter() end
end

-- Present custom counts within this one call, never on the shared document/UI.
-- Always restore state, including when another plugin raises an error.
function PrintPages:present(object, callback, ...)
    if not self:enabled() then return callback(object, ...) end
    local old_ui, old_document = object.ui, object.document
    local old_page, old_pages = object.pageno, object.pages
    local old_data_pages = object.data and object.data.pages
    local document = proxy(self.ui.document, {
        getPageCount = function() return self.count end,
        hasHiddenFlows = function() return false end,
    })
    object.ui = proxy(self.ui, {
        document = document,
        -- Print-page labels take precedence only in this presentation context.
        pagemap = false,
        getCurrentPage = function() return self:page(self.ui:getCurrentPage()) end,
    })
    object.document = document
    if old_page then object.pageno = self:page(old_page) end
    if old_pages then object.pages = self.count end
    local results = pack(pcall(callback, object, ...))
    object.ui, object.document = old_ui, old_document
    object.pageno, object.pages = old_page, old_pages
    if object.data then object.data.pages = old_data_pages end
    if not results[1] then error(results[2]) end
    return unpack(results, 2, results.n)
end

function PrintPages:install()
    if self.ui.document.info.has_pages then return end -- PDF/CBZ keep fixed pages.
    self.ready = true
    local footer = self.ui.view and self.ui.view.footer
    if footer and not self.footer_installed then
        self.footer_installed = true
        local update = footer.updateFooterText
        footer.updateFooterText = function(f, ...)
            local generate = f.genFooterText
            f.genFooterText = function(inner, ...)
                return self:present(inner, generate, ...)
            end
            local result = pack(pcall(update, f, ...))
            f.genFooterText = generate
            if not result[1] then error(result[2]) end
            return unpack(result, 2, result.n)
        end
    end
    local stats = self.ui.statistics
    if not stats or self.stats_installed then return end
    -- KOReader's page_stat SQL view rescales raw screen-page records to
    -- book.pages. Keep data.pages native when writing raw records, and supply
    -- the custom count as the view's target. Font changes stay safe this way.
    if type(stats.insertDB) ~= "function" then return end
    self.stats_installed = true
    local insert = stats.insertDB
    stats.insertDB = function(s, updated_pagecount)
        return insert(s, self:enabled() and self.count or updated_pagecount)
    end
    if stats.onPageUpdate then
        local update = stats.onPageUpdate
        stats.onPageUpdate = function(s, ...)
            local result = pack(update(s, ...))
            -- Keep the cached totals and average on the same scale as the SQL
            -- view instead of mixing buffered screen pages with print pages.
            if self:enabled() and s.id_curr_book then s:insertDB() end
            return unpack(result, 1, result.n)
        end
    end
    if stats.getCurrentStat then
        local current = stats.getCurrentStat
        stats.getCurrentStat = function(s, ...)
            -- Flush with native page counts before entering presentation mode.
            s:insertDB()
            local save = s.insertDB
            s.insertDB = function() end
            local result = pack(pcall(self.present, self, s, current, ...))
            s.insertDB = save
            if not result[1] then error(result[2]) end
            return unpack(result, 2, result.n)
        end
    end
    if stats.getCurrentBookReadPages then
        local read_pages = stats.getCurrentBookReadPages
        stats.getCurrentBookReadPages = function(s, ...)
            local pages = read_pages(s, ...)
            if not pages or not self:enabled() then return pages end
            -- Book map draws screen pages, not print pages. Convert only the
            -- returned display map; the stored statistics stay on their scale.
            local native, mapped = self.ui.document:getPageCount(), {}
            for page, info in pairs(pages) do
                local first = math.floor((page - 1) * native / self.count) + 1
                local last = math.max(first, math.floor(page * native / self.count))
                for index = first, math.min(native, last) do mapped[index] = info end
            end
            return mapped
        end
    end
    if stats.id_curr_book then stats:insertDB() end
end

return PrintPages
