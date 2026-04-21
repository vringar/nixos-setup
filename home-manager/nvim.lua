-- General
vim.opt.history = 500
vim.opt.autoread = true
vim.api.nvim_create_autocmd({ 'FocusGained', 'BufEnter' }, { command = 'checktime' })

vim.g.mapleader = ','

vim.keymap.set('n', '<leader>w', ':w!<CR>')

vim.api.nvim_create_user_command('W', function()
    vim.cmd('w !sudo tee % > /dev/null')
    vim.cmd('edit!')
end, {})

-- UI
vim.opt.scrolloff = 7
vim.opt.wildmenu = true
vim.opt.wildignore = { '*.o', '*~', '*.pyc', '*/.git/*', '*/.hg/*', '*/.svn/*', '*/.DS_Store' }
vim.opt.cmdheight = 1
vim.opt.hidden = true
vim.opt.backspace = { 'eol', 'start', 'indent' }
vim.opt.whichwrap:append('<,>,h,l')
vim.opt.ignorecase = true
vim.opt.smartcase = true
vim.opt.hlsearch = true
vim.opt.incsearch = true
vim.opt.lazyredraw = true
vim.opt.showmatch = true
vim.opt.matchtime = 2
vim.opt.errorbells = false
vim.opt.visualbell = false
vim.opt.timeoutlen = 500
vim.opt.foldcolumn = '1'
vim.opt.number = true

-- Colors
vim.opt.background = 'dark'
pcall(vim.cmd, 'colorscheme desert')

-- Files
vim.opt.fileformats = { 'unix', 'dos', 'mac' }
vim.opt.backup = false
vim.opt.writebackup = false
vim.opt.swapfile = false

-- Indentation
vim.opt.expandtab = true
vim.opt.smarttab = true
vim.opt.shiftwidth = 4
vim.opt.tabstop = 4
vim.opt.linebreak = true
vim.opt.textwidth = 500
vim.opt.autoindent = true
vim.opt.smartindent = true
vim.opt.wrap = true

-- Status line
vim.opt.laststatus = 2
_G.HasPaste = function()
    if vim.opt.paste:get() then return 'PASTE MODE  ' end
    return ''
end
vim.opt.statusline = ' %{v:lua.HasPaste()}%F%m%r%h %w  CWD: %r%{getcwd()}%h   Line: %l  Column: %c'

-- Keymaps
local map = vim.keymap.set

map({ 'n', 'v' }, '<space>', '/')
map({ 'n', 'v' }, '<C-space>', '?')
map('n', '<leader><CR>', ':noh<CR>', { silent = true })

map('n', '<C-j>', '<C-W>j')
map('n', '<C-k>', '<C-W>k')
map('n', '<C-h>', '<C-W>h')
map('n', '<C-l>', '<C-W>l')

map('n', '<leader>bd', ':Bclose<CR>:tabclose<CR>gT')
map('n', '<leader>ba', ':bufdo bd<CR>')
map('n', '<leader>l', ':bnext<CR>')
map('n', '<leader>h', ':bprevious<CR>')

map('n', '<leader>tn', ':tabnew<CR>')
map('n', '<leader>to', ':tabonly<CR>')
map('n', '<leader>tc', ':tabclose<CR>')
map('n', '<leader>tm', ':tabmove ')
map('n', '<leader>t<leader>', ':tabnext ')

vim.g.lasttab = 1
map('n', '<Leader>tl', function() vim.cmd('tabn ' .. vim.g.lasttab) end)
vim.api.nvim_create_autocmd('TabLeave', {
    callback = function() vim.g.lasttab = vim.fn.tabpagenr() end,
})

map('n', '<leader>te', ':tabedit <C-r>=expand("%:p:h")<CR>/')
map('n', '<leader>cd', ':cd %:p:h<CR>:pwd<CR>')

vim.opt.switchbuf = { 'useopen', 'usetab', 'newtab' }
vim.opt.showtabline = 2

vim.api.nvim_create_autocmd('BufReadPost', {
    callback = function()
        local mark = vim.api.nvim_buf_get_mark(0, '"')
        local lcount = vim.api.nvim_buf_line_count(0)
        if mark[1] > 1 and mark[1] <= lcount then
            vim.api.nvim_win_set_cursor(0, mark)
        end
    end,
})

map('n', '0', '^')

map('n', '<M-j>', 'mz:m+<CR>`z')
map('n', '<M-k>', 'mz:m-2<CR>`z')
map('v', '<M-j>', ":m'>+<CR>`<my`>mzgv`yo`z")
map('v', '<M-k>', ":m'<-2<CR>`>my`<mzgv`yo`z")

local function clean_extra_spaces()
    local cursor = vim.fn.getpos('.')
    local query = vim.fn.getreg('/')
    vim.cmd([[silent! %s/\s\+$//e]])
    vim.fn.setpos('.', cursor)
    vim.fn.setreg('/', query)
end
vim.api.nvim_create_autocmd('BufWritePre', {
    pattern = { '*.txt', '*.js', '*.py', '*.wiki', '*.sh', '*.coffee' },
    callback = clean_extra_spaces,
})

map('v', '*', function()
    vim.cmd('normal! vgvy')
    local pattern = vim.fn.escape(vim.fn.getreg('"'), "\\/.*'$^~[]")
    pattern = pattern:gsub('\n$', '')
    vim.fn.setreg('/', pattern)
    vim.cmd('/' .. pattern)
end, { silent = true })

map('v', '#', function()
    vim.cmd('normal! vgvy')
    local pattern = vim.fn.escape(vim.fn.getreg('"'), "\\/.*'$^~[]")
    pattern = pattern:gsub('\n$', '')
    vim.fn.setreg('/', pattern)
    vim.cmd('?' .. pattern)
end, { silent = true })

map('n', '<leader>ss', ':setlocal spell!<CR>')
map('n', '<leader>sn', ']s')
map('n', '<leader>sp', '[s')
map('n', '<leader>sa', 'zg')
map('n', '<leader>s?', 'z=')

map('n', '<leader>q', ':e ~/buffer<CR>')
map('n', '<leader>x', ':e ~/buffer.md<CR>')
map('n', '<leader>pp', ':setlocal paste!<CR>')

local function bclose()
    local current = vim.fn.bufnr('%')
    local alternate = vim.fn.bufnr('#')
    if vim.fn.buflisted(alternate) == 1 then
        vim.cmd('buffer #')
    else
        vim.cmd('bnext')
    end
    if vim.fn.bufnr('%') == current then
        vim.cmd('new')
    end
    if vim.fn.buflisted(current) == 1 then
        vim.cmd('bdelete! ' .. current)
    end
end
vim.api.nvim_create_user_command('Bclose', bclose, {})
