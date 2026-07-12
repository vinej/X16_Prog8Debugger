// X16 Prog8 Debugger -- VSCode glue. No build step, no npm dependencies:
// the debug adapter is Python (tools/dap_adapter.py) and everything else
// is declared in package.json. This file wires the adapter factory, a
// default F5 configuration, and lightweight completions for .p8 files.
const vscode = require('vscode');
const path = require('path');

const KEYWORDS = [
    'sub', 'asmsub', 'romsub', 'inline', 'const', 'return', 'break', 'continue',
    'if', 'else', 'when', 'for', 'in', 'to', 'downto', 'step', 'while', 'do',
    'until', 'repeat', 'unroll', 'goto', 'and', 'or', 'not', 'xor', 'as', 'void',
    'true', 'false', 'clobbers', 'defer', 'on'
];
const TYPES = ['byte', 'ubyte', 'word', 'uword', 'long', 'float', 'str', 'bool'];
const DIRECTIVES = [
    '%import', '%zeropage', '%option', '%address', '%memtop', '%output',
    '%launcher', '%encoding', '%align', '%asm', '%asmbinary', '%asminclude',
    '%breakpoint', '%zpallowed', '%zpreserved'
];
const BUILTINS = [
    'abs', 'clamp', 'len', 'lsb', 'msb', 'lsw', 'msw', 'min', 'max', 'mkword',
    'peek', 'peekw', 'poke', 'pokew', 'rol', 'rol2', 'ror', 'ror2', 'setlsb',
    'setmsb', 'sgn', 'sizeof', 'sqrt', 'divmod', 'memory', 'call', 'callfar'
];
// common members of the library modules bounce.p8-style programs import
const MODULES = {
    txt: ['print', 'print_ub', 'print_uw', 'print_w', 'print_b', 'nl', 'spc',
        'chrout', 'clear_screen', 'home', 'plot', 'column', 'row', 'color',
        'lowercase', 'uppercase', 'setchr', 'getchr', 'input_chars'],
    sys: ['wait', 'waitvsync', 'reset_system', 'exit', 'memcopy', 'memset',
        'memsetw', 'rsave', 'rrestore', 'set_irq', 'restore_irq'],
    cx16: ['vpoke', 'vpeek', 'vaddr', 'vpoke_or', 'vpoke_and', 'vpoke_xor',
        'rombank', 'rambank', 'push_rombank', 'pop_rombank', 'getrambank',
        'VERA_DATA0', 'VERA_DATA1', 'VERA_CTRL', 'r0', 'r1', 'r2', 'r3',
        'r4', 'r5', 'r6', 'r7', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13',
        'r14', 'r15'],
    sprites: ['init', 'pos', 'posxy', 'hide', 'show', 'flipx', 'flipy',
        'SIZE_16', 'SIZE_32', 'SIZE_64', 'SIZE_8', 'COLORS_16', 'COLORS_256'],
    psg: ['init', 'silent', 'voice', 'freq', 'volume', 'pulse_width', 'envelope',
        'LEFT', 'RIGHT', 'TRIANGLE', 'SAWTOOTH', 'PULSE', 'NOISE'],
    conv: ['str_ub', 'str_uw', 'str_w', 'str_b', 'hex_uw', 'bin_uw'],
    string: ['length', 'copy', 'compare', 'find', 'contains', 'lower', 'upper'],
    math: ['sin8u', 'cos8u', 'rnd', 'rndw', 'atan2', 'crc16', 'diff', 'log2'],
    cbm: ['GETIN2', 'CHRIN', 'CHROUT', 'SETTIM', 'RDTIM16', 'kbdbuf_clear'],
    floats: ['print', 'sin', 'cos', 'tan', 'atan', 'ln', 'log2', 'sqrt',
        'round', 'floor', 'ceil', 'rnd', 'PI', 'TWOPI']
};

function item(label, kind, detail) {
    const it = new vscode.CompletionItem(label, kind);
    if (detail) it.detail = detail;
    return it;
}

function activate(context) {
    context.subscriptions.push(
        vscode.debug.registerDebugAdapterDescriptorFactory('prog8', {
            createDebugAdapterDescriptor(session) {
                const python = (session.configuration && session.configuration.python) || 'python';
                const adapter = context.asAbsolutePath(path.join('tools', 'dap_adapter.py'));
                return new vscode.DebugAdapterExecutable(python, [adapter]);
            }
        }),

        // F5 on a .p8 file without a launch.json still works
        vscode.debug.registerDebugConfigurationProvider('prog8', {
            resolveDebugConfiguration(folder, config) {
                if (!config.type && !config.request && !config.name) {
                    const editor = vscode.window.activeTextEditor;
                    if (editor && editor.document.languageId === 'prog8') {
                        config.type = 'prog8';
                        config.request = 'launch';
                        config.name = 'Debug Prog8 program';
                        config.program = editor.document.fileName;
                        config.stopOnEntry = true;
                        if (folder) config.cwd = folder.uri.fsPath;
                    }
                }
                if (!config.program) {
                    vscode.window.showErrorMessage('prog8 debug: no .p8 program to launch');
                    return undefined;
                }
                return config;
            }
        }),

        vscode.languages.registerCompletionItemProvider('prog8', {
            provideCompletionItems(document, position) {
                const line = document.lineAt(position).text.slice(0, position.character);
                const member = line.match(/([A-Za-z_][A-Za-z0-9_]*)\.$/);
                if (member) {
                    const members = MODULES[member[1]];
                    if (!members) return undefined;
                    return members.map(m => item(m,
                        m === m.toUpperCase()
                            ? vscode.CompletionItemKind.Constant
                            : vscode.CompletionItemKind.Function,
                        member[1] + '.' + m));
                }
                const out = [];
                KEYWORDS.forEach(k => out.push(item(k, vscode.CompletionItemKind.Keyword)));
                TYPES.forEach(t => out.push(item(t, vscode.CompletionItemKind.Struct, 'prog8 type')));
                BUILTINS.forEach(b => out.push(item(b, vscode.CompletionItemKind.Function, 'built-in')));
                Object.keys(MODULES).forEach(m =>
                    out.push(item(m, vscode.CompletionItemKind.Module, 'library module')));
                if (line.trimStart().startsWith('%') || line.trimStart() === '') {
                    DIRECTIVES.forEach(d => {
                        const it = item(d, vscode.CompletionItemKind.Event, 'directive');
                        it.insertText = line.trimStart().startsWith('%') ? d.slice(1) : d;
                        out.push(it);
                    });
                }
                return out;
            }
        }, '.', '%')
    );
}

function deactivate() { }

module.exports = { activate, deactivate };
