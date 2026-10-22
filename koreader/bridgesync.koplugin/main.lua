--[[
Bridge sync: pull this device's queue from ereader-bridge (GET /pull/<device>), so files arrive
even where the bridge cannot push (VPN, phone hotspot), and keep this plugin up to date from the bridge.

- On start / wake-up, if new files are queued: [Get all] / [Choose] / [Later].
  [Later] stays quiet until another file is queued.
- Each file downloads in a subprocess, so the UI stays responsive; tap the progress box to stop.
- On start (once per session) it compares its version with the bridge's copy and offers to update.
- Tools menu → Bridge sync: queue / update check.

Settings: <koreader>/settings/bridgesync.lua
    return { ["url"] = "http://<bridge-host>:8086", ["device"] = "<device-name>", ["dir"] = "/storage/emulated/0/Books" }
]]
local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local InfoMessage = require("ui/widget/infomessage")
local JSON = require("json")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local NetworkMgr = require("ui/network/manager")
local Notification = require("ui/widget/notification")
local Screen = require("device").screen
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local ffiutil = require("ffi/util")
local http = require("socket.http")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local ltn12 = require("ltn12")
local sha2 = require("ffi/sha2")
local socket = require("socket")
local socketutil = require("socketutil")
local util = require("util")
local T = ffiutil.template

local MAX_LISTED = 12
-- KOReader can hold several instances of a plugin (file manager, reader, Bookshelf...);
-- this module-level state makes them behave as one.
local shared = { busy = false, asking = false, last_auto = 0, update_checked = false }

local BridgeSync = WidgetContainer:extend{
    name = "bridgesync",
}

function BridgeSync:init()
    self.settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/bridgesync.lua")
    self.url = self.settings:readSetting("url")
    self.device = self.settings:readSetting("device")
    self.dir = self.settings:readSetting("dir")
    local ok, meta = pcall(dofile, self.path .. "/_meta.lua")
    self.version = ok and meta.version or "0"
    self.auto = function() self:check(false) end
    self.auto_update = function() self:checkUpdate(false) end
    self.ui.menu:registerToMainMenu(self)
end

function BridgeSync:addToMainMenu(menu_items)
    menu_items.bridgesync = {
        text = "Bridge sync",
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = "Show queue",
                callback = function() NetworkMgr:runWhenOnline(function() self:check(true) end) end,
            },
            {
                text_func = function() return T("Check for plugin update (current %1)", self.version) end,
                callback = function() NetworkMgr:runWhenOnline(function() self:checkUpdate(true) end) end,
            },
        },
    }
end

-- Wi-Fi may need a few seconds after wake-up; an unreachable bridge costs one short, silent request.
function BridgeSync:onNetworkConnected()
    UIManager:scheduleIn(2, self.auto)
    if not shared.update_checked then UIManager:scheduleIn(4, self.auto_update) end
end
function BridgeSync:onResume() UIManager:scheduleIn(5, self.auto) end
function BridgeSync:onCloseWidget()
    UIManager:unschedule(self.auto)
    UIManager:unschedule(self.auto_update)
end

local function get(url, sink, block_timeout)
    socketutil:set_timeout(block_timeout, -1) -- no total limit: big files over VPN take a while
    local code = socket.skip(1, http.request{ url = url, sink = sink })
    socketutil:reset_timeout()
    return code
end

local function getJSON(url)
    local body = {}
    if get(url, ltn12.sink.table(body), 3) ~= 200 then return nil end
    return JSON.decode(table.concat(body))
end

function BridgeSync:base()
    return self.url .. "/pull/" .. self.device
end

function BridgeSync:configured(manual)
    if self.url and self.device and self.dir then return true end
    if manual then UIManager:show(InfoMessage:new{ text = "Bridge sync is not configured (settings/bridgesync.lua)." }) end
    return false
end

---------------------------------------------------------------- queue

function BridgeSync:check(manual)
    if not self:configured(manual) or shared.busy or shared.asking then return end
    if not manual then
        if os.time() - shared.last_auto < 10 then return end -- start and wake-up can both fire
        shared.last_auto = os.time()
    end
    local ok, data = pcall(getJSON, self:base())
    local jobs = ok and data and data.jobs
    if not jobs then
        if manual then UIManager:show(InfoMessage:new{ text = "Cannot reach the bridge." }) end
        return -- not on a network that reaches the bridge; try again next wake-up
    end
    if #jobs == 0 then
        if manual then Notification:notify("Bridge: no files queued", Notification.SOURCE_ALWAYS_SHOW) end
        return
    end
    -- "Later" remembers the batch; only a newly queued file asks again
    local seen = {}
    for _, id in ipairs(self.settings:readSetting("dismissed") or {}) do seen[id] = true end
    local has_new = false
    for _, job in ipairs(jobs) do has_new = has_new or not seen[job.job] end
    if not manual and not has_new then return end
    self:askQueue(jobs)
end

local function totalSize(jobs)
    local total = 0
    for _, job in ipairs(jobs) do total = total + job.size end
    return total
end

function BridgeSync:askQueue(jobs)
    local lines = {}
    for i, job in ipairs(jobs) do
        if i > MAX_LISTED then
            table.insert(lines, T("… and %1 more", #jobs - MAX_LISTED))
            break
        end
        table.insert(lines, "• " .. job.file .. "  (" .. util.getFriendlySize(job.size) .. ")")
    end
    shared.asking = true
    UIManager:show(ConfirmBox:new{
        text = T("Bridge queue: %1 file(s), %2\n\n%3", #jobs, util.getFriendlySize(totalSize(jobs)), table.concat(lines, "\n")),
        ok_text = "Get all",
        cancel_text = "Later",
        ok_callback = function()
            shared.asking = false
            UIManager:nextTick(function() self:download(jobs) end)
        end,
        cancel_callback = function() -- also tapping outside the box
            shared.asking = false
            local ids = {}
            for _, job in ipairs(jobs) do table.insert(ids, job.job) end
            self.settings:saveSetting("dismissed", ids)
            self.settings:flush()
        end,
        other_buttons_first = true,
        other_buttons = {{
            {
                text = "Choose",
                callback = function()
                    shared.asking = false
                    UIManager:nextTick(function() self:pick(jobs) end)
                end,
            },
        }},
    })
end

-- Full-screen checklist: tap a file to toggle it, tap the first row to download the checked ones.
function BridgeSync:pick(jobs)
    local checked = {}
    for i = 1, #jobs do checked[i] = true end
    local menu
    local function items()
        local chosen = {}
        for i, job in ipairs(jobs) do if checked[i] then table.insert(chosen, job) end end
        local t = {{
            text = T("▶ Get %1 selected (%2)", #chosen, util.getFriendlySize(totalSize(chosen))),
            bold = true,
            callback = function()
                UIManager:close(menu)
                if #chosen > 0 then UIManager:nextTick(function() self:download(chosen) end) end
            end,
        }}
        for i, job in ipairs(jobs) do
            table.insert(t, {
                text = (checked[i] and "☑ " or "☐ ") .. job.file,
                mandatory = util.getFriendlySize(job.size),
                callback = function()
                    checked[i] = not checked[i]
                    menu:switchItemTable(nil, items(), i + 1)
                end,
            })
        end
        return t
    end
    menu = Menu:new{
        title = "Choose files to get",
        item_table = items(),
        width = Screen:getWidth(),
        height = Screen:getHeight(),
        is_borderless = true,
        is_popout = false,
        covers_fullscreen = true,
    }
    UIManager:show(menu)
end

function BridgeSync:download(jobs)
    if shared.busy then return end
    shared.busy = true
    Trapper:wrap(function()
        local got, got_paths, stopped = {}, {}, false
        for i, job in ipairs(jobs) do
            local name = job.file:gsub("[/\\]", "_")
            local final = self.dir .. "/" .. name
            -- already here (pushed meanwhile, or an earlier "done" got lost): just confirm it
            if lfs.attributes(final, "size") ~= job.size then
                local part = self.dir .. "/." .. name .. ".part"
                local url = self:base() .. "/" .. job.job
                -- subprocess: the UI keeps running; tapping the box kills the download
                local completed, code = Trapper:dismissableRunInSubprocess(function()
                    return tostring(get(url, socketutil.file_sink(io.open(part, "wb")), socketutil.FILE_BLOCK_TIMEOUT))
                end, T("Bridge: getting %1/%2 (tap to stop)\n\n%3\n%4", i, #jobs, name, util.getFriendlySize(job.size)), true)
                if not completed then
                    os.remove(part)
                    stopped = true
                    -- free the job on the bridge now, instead of after the 15 min claim timeout
                    socketutil:set_timeout(3, 10)
                    socket.skip(1, http.request{ url = url .. "/release", method = "POST",
                                                 headers = { ["Content-Length"] = "0" }, source = ltn12.source.string("") })
                    socketutil:reset_timeout()
                    break
                end
                if code ~= "200" or lfs.attributes(part, "size") ~= job.size then
                    os.remove(part) -- 404/409: pushed or being pushed right now; anything else retries next time
                    logger.warn("BridgeSync: skipped", name, code)
                    goto continue
                end
                os.rename(part, final)
            end
            socketutil:set_timeout(3, 10)
            socket.skip(1, http.request{ url = self:base() .. "/" .. job.job .. "/done", method = "POST",
                                         headers = { ["Content-Length"] = "0" }, source = ltn12.source.string("") })
            socketutil:reset_timeout()
            table.insert(got, name)
            table.insert(got_paths, final)
            ::continue::
        end
        shared.busy = false
        local text = T("Bridge: got %1/%2", #got, #jobs)
        if stopped then
            text = text .. " (stopped; the rest stay queued)"
        elseif #got < #jobs then
            text = text .. " (the rest will be retried later)"
        end
        pcall(function()
            local FileManager = require("apps/filemanager/filemanager")
            if FileManager.instance then FileManager.instance:onRefresh() end
        end)
        if #got_paths == 1 and not stopped then -- a single book: offer to open it
            UIManager:show(ConfirmBox:new{
                text = T("Got '%1'.\n\nOpen it now?", got[1]),
                ok_text = "Open",
                cancel_text = "Later",
                ok_callback = function()
                    UIManager:nextTick(function()
                        require("apps/reader/readerui"):showReader(got_paths[1])
                    end)
                end,
            })
        else
            Notification:notify(text, Notification.SOURCE_ALWAYS_SHOW)
        end
    end)
end

---------------------------------------------------------------- self-update

local function newer(a, b) -- "1.10.0" > "1.9.2"
    local pa, pb = {}, {}
    for n in tostring(a):gmatch("%d+") do table.insert(pa, tonumber(n)) end
    for n in tostring(b):gmatch("%d+") do table.insert(pb, tonumber(n)) end
    for i = 1, math.max(#pa, #pb) do
        if (pa[i] or 0) ~= (pb[i] or 0) then return (pa[i] or 0) > (pb[i] or 0) end
    end
    return false
end

function BridgeSync:checkUpdate(manual)
    if not manual and shared.update_checked then return end -- every plugin instance schedules one
    shared.update_checked = true
    if not self:configured(manual) then return end
    local ok, m = pcall(getJSON, T("%1/plugin/bridgesync/manifest?device=%2&version=%3", self.url, self.device, self.version))
    if not (ok and m and m.version) then
        if manual then UIManager:show(InfoMessage:new{ text = "Cannot reach the bridge." }) end
        return
    end
    if not newer(m.version, self.version) then
        if manual then Notification:notify(T("Bridge sync %1 is up to date", self.version), Notification.SOURCE_ALWAYS_SHOW) end
        return
    end
    if not manual and self.settings:readSetting("skip_version") == m.version then return end
    UIManager:show(ConfirmBox:new{
        text = T("A Bridge sync update is available.\n%1 → %2\n\nInstall now? It takes effect after KOReader restarts.", self.version, m.version),
        ok_text = "Update",
        cancel_text = "Later",
        ok_callback = function() UIManager:nextTick(function() self:install(m) end) end,
        cancel_callback = function()
            self.settings:saveSetting("skip_version", m.version)
            self.settings:flush()
        end,
    })
end

-- Download every file into <plugin>.new, verify sha256, then swap directories.
function BridgeSync:install(m)
    local staging, old = self.path .. ".new", self.path .. ".old"
    ffiutil.purgeDir(staging)
    local ok, err = pcall(function()
        for _, f in ipairs(m.files) do
            local dest = staging .. "/" .. f.path
            util.makePath(dest:match("^(.*)/[^/]*$"))
            local body = {}
            if get(self.url .. "/plugin/bridgesync/" .. f.path, ltn12.sink.table(body), 10) ~= 200 then
                error("download failed: " .. f.path)
            end
            local data = table.concat(body)
            if #data ~= f.size or sha2.sha256(data) ~= f.sha256 then error("checksum mismatch: " .. f.path) end
            local fh = assert(io.open(dest, "wb"))
            fh:write(data)
            fh:close()
        end
    end)
    if not ok then
        ffiutil.purgeDir(staging)
        logger.warn("BridgeSync: update failed:", err)
        UIManager:show(InfoMessage:new{ text = "Bridge sync update failed. Will try again later." })
        return
    end
    ffiutil.purgeDir(old)
    assert(os.rename(self.path, old))
    if not os.rename(staging, self.path) then
        os.rename(old, self.path) -- put the working version back
        UIManager:show(InfoMessage:new{ text = "Bridge sync update failed." })
        return
    end
    ffiutil.purgeDir(old)
    self.settings:delSetting("skip_version")
    self.settings:flush()
    UIManager:askForRestart(T("Bridge sync %1 installed. Restart KOReader to use it.", m.version))
end

return BridgeSync
