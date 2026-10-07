// Register map and fixed hardware constants. Mirror of test/regs.py: keep in sync.
//
// Config writes arrive as (address byte, data byte) pairs. Write everything
// with CTRL.enable = 0, then set enable; the core starts at the next frame.

localparam [7:0] REG_CTRL      = 8'h00;  // [1:0] mode, [7] enable
localparam [7:0] REG_N_SEL     = 8'h01;  // number of selected channels, 1..N_SEL
localparam [7:0] REG_FPP       = 8'h02;  // frames per packet, broadband modes (1..255)
localparam [7:0] REG_WIN_LO    = 8'h03;  // window length in frames, spike modes [7:0]
localparam [7:0] REG_WIN_HI    = 8'h04;  //                                       [15:8]
localparam [7:0] REG_SBP_SHIFT = 8'h05;  // [4:0] spike-band power output shift
localparam [7:0] REG_SEL_SLOT  = 8'h10;  // +i: ADC slot of selected channel i (strictly ascending)
localparam [7:0] REG_THR       = 8'h20;  // +i: spike threshold of selected channel i

localparam [1:0] MODE_LOSSLESS = 2'd0;
localparam [1:0] MODE_LOSSY    = 2'd1;
localparam [1:0] MODE_BINNED   = 2'd2;
localparam [1:0] MODE_SBP      = 2'd3;
