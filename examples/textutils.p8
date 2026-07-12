; Imported module for the multi-file debugging test (see multi.p8).

%import textio

textutils {
    uword calls = 0

    sub announce(ubyte n) {
        calls++
        txt.print("call ")
        txt.print_ub(n)
        txt.nl()
    }
}
