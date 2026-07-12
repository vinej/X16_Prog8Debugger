; =====================================================================
; x16 bounce example -- Prog8 port
; =====================================================================
; A frame-locked sprite bouncing around the 640x480 screen, driven by
; 8.8-style fixed-point velocity, with AABB collision against a target
; box drawn near the middle of the screen.
;
; Sound: a PSG blip on every wall bounce with a per-frame volume decay;
; a sustained PSG tone while the sprite overlaps the target box.
; (The C/assembly versions of this demo use the YM2151 for the overlap
; note; here it is a second PSG voice -- driving the YM needs the
; KERNAL audio-bank API and would only add noise to a toolchain test.)
;
; Exercises: VSYNC frame lock, sprites, VRAM writes, fixed-point-ish
; movement, AABB collision, PSG, tilemap text.
;
; Build (see .vscode/tasks.json):
;   java -jar prog8c.jar -target cx16 -asmlist -out build examples/bounce.p8
;
; Press any key to stop.
; =====================================================================

%import syslib
%import textio
%import sprites
%import psg
%zeropage basicsafe

main {
    ; sprite image: 16x16, 4bpp, at VRAM $13000 (the KERNAL sprite area)
    const ubyte SPR_BANK  = 1
    const uword SPR_ADDR  = $3000
    const ubyte SPR_NUM   = 1          ; sprite 0 is the mouse pointer

    ; display is 640x480 in the default 80x60 text mode
    const word PLAY_W = 640
    const word PLAY_H = 480
    const word SPR_SIZE = 16

    ; target box, in text cells (80x60 grid) and display pixels
    const ubyte BOX_COL = 34
    const ubyte BOX_ROW = 26
    const ubyte BOX_W   = 12
    const ubyte BOX_H   = 8
    const word  BOX_X1  = BOX_COL as word * 8
    const word  BOX_Y1  = BOX_ROW as word * 8
    const word  BOX_X2  = BOX_X1 + BOX_W as word * 8 - 1
    const word  BOX_Y2  = BOX_Y1 + BOX_H as word * 8 - 1

    ; position in pixels, velocity as integer + 1/256 fraction parts
    word  pos_x = 96
    word  pos_y = 64
    byte  dir_x = 1                    ; +1 or -1
    byte  dir_y = 1
    ubyte vint_x = 2                   ; velocity, integer pixels/frame
    ubyte vfrac_x = 170                ; velocity, fraction (n/256 px/frame)
    ubyte vint_y = 1
    ubyte vfrac_y = 220
    ubyte acc_x = 0                    ; fraction accumulators
    ubyte acc_y = 0

    ubyte blip_vol = 0                 ; PSG blip decay envelope
    bool  in_box = false
    uword bounces = 0

    sub start() {
        txt.clear_screen()
        txt.print("prog8 bounce - any key stops\n")
        draw_box()
        build_sprite_image()

        sprites.init(SPR_NUM, SPR_BANK, SPR_ADDR,
                     sprites.SIZE_16, sprites.SIZE_16,
                     sprites.COLORS_16, 0)

        psg.init()
        ; voice 0: wall-bounce blip (triangle), voice 1: in-box tone (saw)
        psg.voice(0, psg.LEFT | psg.RIGHT, 0, psg.TRIANGLE, 0)
        psg.voice(1, psg.LEFT | psg.RIGHT, 0, psg.SAWTOOTH, 0)
        psg.freq(1, 400)

        repeat {
            sys.waitvsync()
            move_axis_x()
            move_axis_y()
            sprites.pos(SPR_NUM, pos_x, pos_y)
            check_box()
            decay_blip()
            if cbm.GETIN2() != 0
                break
        }

        psg.silent()
        sprites.hide(SPR_NUM)
        txt.print("\nbounces: ")
        txt.print_uw(bounces)
        txt.nl()
    }

    sub move_axis_x() {
        ; 8.8-ish fixed point: accumulate the fraction, carry into pixels
        uword acc = acc_x as uword + vfrac_x
        acc_x = lsb(acc)
        word step = (vint_x + msb(acc)) as word
        if dir_x < 0
            step = -step
        pos_x += step
        if pos_x <= 0 {
            pos_x = 0
            dir_x = 1
            blip(1400)
        } else if pos_x >= PLAY_W - SPR_SIZE {
            pos_x = PLAY_W - SPR_SIZE
            dir_x = -1
            blip(1400)
        }
    }

    sub move_axis_y() {
        uword acc = acc_y as uword + vfrac_y
        acc_y = lsb(acc)
        word step = (vint_y + msb(acc)) as word
        if dir_y < 0
            step = -step
        pos_y += step
        if pos_y <= 0 {
            pos_y = 0
            dir_y = 1
            blip(1000)
        } else if pos_y >= PLAY_H - SPR_SIZE {
            pos_y = PLAY_H - SPR_SIZE
            dir_y = -1
            blip(1000)
        }
    }

    sub blip(uword vera_freq) {
        bounces++
        blip_vol = 63
        psg.freq(0, vera_freq)
        psg.volume(0, blip_vol)
    }

    sub decay_blip() {
        if blip_vol != 0 {
            blip_vol -= 3
            if blip_vol < 3
                blip_vol = 0
            psg.volume(0, blip_vol)
        }
    }

    sub check_box() {
        ; AABB overlap sprite vs target box, in display pixels
        bool hit = pos_x + SPR_SIZE - 1 >= BOX_X1 and pos_x <= BOX_X2
               and pos_y + SPR_SIZE - 1 >= BOX_Y1 and pos_y <= BOX_Y2
        if hit and not in_box {
            in_box = true
            psg.volume(1, 40)
        } else if not hit and in_box {
            in_box = false
            psg.volume(1, 0)
        }
    }

    sub draw_box() {
        ; solid box on the text layer: screen code $A0 (reverse space),
        ; color byte $61 (white on blue-ish). Layer-1 map at VRAM $1B000,
        ; 256 bytes per row (128 tiles * 2).
        ubyte row
        ubyte col
        for row in BOX_ROW to BOX_ROW + BOX_H - 1 {
            uword vaddr = $B000 + row as uword * 256 + BOX_COL as uword * 2
            for col in 0 to BOX_W - 1 {
                cx16.vpoke(1, vaddr, $A0)
                cx16.vpoke(1, vaddr + 1, $61)
                vaddr += 2
            }
        }
    }

    sub build_sprite_image() {
        ; 16x16 4bpp ball: filled circle-ish diamond, color index 7,
        ; darker rim color 8. 8 bytes per row (2 pixels/byte).
        ubyte[16] widths = [ 4, 7, 10, 12, 13, 14, 15, 16,
                             16, 15, 14, 13, 12, 10, 7, 4 ]
        cx16.vaddr(SPR_BANK, SPR_ADDR, 0, 1)   ; data port 0, auto-inc 1
        ubyte y
        for y in 0 to 15 {
            ubyte w = widths[y]
            ubyte left = (16 - w) >> 1
            ubyte x
            for x in 0 to 15 step 2 {
                ubyte pix = 0
                if x >= left and x < left + w
                    pix = $70              ; left pixel of the pair
                if x + 1 >= left and x + 1 < left + w
                    pix |= $07             ; right pixel of the pair
                cx16.VERA_DATA0 = pix
            }
        }
    }
}
