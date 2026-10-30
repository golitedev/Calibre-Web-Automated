-- Latest unsent position per book/account. No passwords or Inkly credentials.
local Queue = {}
Queue.__index = Queue
local KEY = "cwa_pending_uploads"
local logger = require("logger")
local running = false -- shared across reader/file-manager instances

function Queue.new(settings)
    return setmetatable({ settings = settings }, Queue)
end

function Queue:key(item)
    return item.server .. "\n" .. item.username .. "\n" .. item.document .. "\n" .. item.day
end

function Queue:items()
    return self.settings:readSetting(KEY, {})
end

function Queue:save(items)
    self.settings:saveSetting(KEY, items)
    local ok = pcall(self.settings.flush, self.settings)
    if not ok then logger.warn("Could not persist pending CWA uploads; keeping them in memory") end
end

function Queue:put(item)
    local items = self:items()
    local key = self:key(item)
    local old = items[key]
    if old and old.progress == item.progress and old.percentage == item.percentage
            and old.timestamp == item.timestamp then return old end
    items[key] = item
    self:save(items)
    return item
end

function Queue:flush(server, username, send, done)
    if running then return false end
    running = true
    local attempted = {}
    local function pending_batch()
        local batch = {}
        for key, item in pairs(self:items()) do
            if item.server == server and item.username == username and not attempted[item.id] then
                batch[#batch + 1] = { key = key, item = item }
            end
        end
        table.sort(batch, function(a, b) return a.item.timestamp < b.item.timestamp end)
        return batch
    end
    local batch = pending_batch()
    local index, failed = 0, false
    local function next_item()
        index = index + 1
        local entry = batch[index]
        if not entry then
            -- Closing the reader may queue a final position during an upload.
            -- Drain that newer snapshot too; a cached file manager need not
            -- receive another Wi-Fi event for it to reach CWA.
            batch = pending_batch()
            index = 0
            if #batch > 0 then next_item(); return end
            running = false
            if done then done(not failed) end
            return
        end
        attempted[entry.item.id] = true
        local completed = false
        local function acknowledge(ok)
            if completed then return end
            completed = true
            if ok then
                local items = self:items()
                -- Reading may have advanced while this request was in flight.
                if items[entry.key] and items[entry.key].id == entry.item.id then
                    items[entry.key] = nil
                    self:save(items)
                end
            else
                failed = true
            end
            next_item()
        end
        local ok = pcall(send, entry.item, acknowledge)
        if not ok then acknowledge(false) end
    end
    next_item()
    return true
end

return Queue
