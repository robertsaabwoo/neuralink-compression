// Register map. Mirror of test/regs.py: keep in sync.
//
// Config writes arrive as (address byte, data byte) pairs. Write everything
// with CTRL.enable = 0, then set enable; the core starts at the next frame.
// Silicon is lossy mode only (decision D1): CTRL[1:0] (mode) is ignored; hosts
// write MODE_LOSSY there for compatibility with the packet header's mode field.

localparam [7:0] REG_CTRL      = 8'h00;  // [7] enable, [1:0] mode (ignored, write MODE_LOSSY)
localparam [7:0] REG_N_SEL     = 8'h01;  // number of selected channels, 1..N_SEL
localparam [7:0] REG_SEL_SLOT  = 8'h10;  // +i: ADC slot of selected channel i (strictly ascending)

localparam [1:0] MODE_LOSSY    = 2'd1;
