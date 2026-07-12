; Multi-file debugging test: breakpoints/stepping in an %imported module
; (textutils.p8) and a %breakpoint directive that the debugger must sync.

%import textio
%import textutils
%zeropage basicsafe

main {
    ubyte counter = 0

    sub start() {
        txt.print("multi-file test\n")
        repeat 5 {
            counter++
            textutils.announce(counter)
        }
        %breakpoint
        txt.print("done\n")
        repeat {
        }
    }
}
