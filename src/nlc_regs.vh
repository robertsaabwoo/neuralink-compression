// Register map. Mirror of test/regs.py: keep in sync.
//
// Config writes arrive as (address byte, data byte) pairs. Write everything
// with CTRL.enable = 0, then set enable; the core starts at the next frame.
// Silicon is lossy mode only (decision D1): CTRL[1:0] (mode) is ignored; hosts
// write MODE_LOSSY there for compatibility with the packet header's mode field.

localparam [7:0] REG_CTRL      = 8'h00;  // [7] enable, [1:0] mode (ignored, write MODE_LOSSY)
localparam [7:0] REG_N_SEL     = 8'h01;  // number of selected channels, 1..N_SEL
localparam [7:0] REG_DBG       = 8'h08;  // DFT (D11): [1:0] mode, [7:4] gate group (mode 2).
                                         // Written only while enable = 0 (ignored otherwise)
localparam [7:0] REG_SEL_SLOT  = 8'h10;  // +i: ADC slot of selected channel i (strictly ascending)

localparam [1:0] MODE_LOSSY    = 2'd1;

localparam [1:0] DBG_NORMAL    = 2'd0;   // compressed packets (3: reserved, = normal)
localparam [1:0] DBG_RAW       = 2'd1;   // raw bypass: selected samples, 2 bytes each
localparam [1:0] DBG_ICG       = 2'd2;   // clock-gate enables of group [7:4] on uo_out
