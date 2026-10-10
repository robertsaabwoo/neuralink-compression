`default_nettype none

// Tiny Tapeout top level: pins <-> nlc_core.
// Rename the module to tt_um_<github username>_nlc (must be unique on the shuttle)
// and update info.yaml and test/tb.v to match.
//
// Pin map:
//   ui_in[7:0]  in   sample[7:0], or a config byte while cfg_en = 1
//   uio[1:0]    in   sample[9:8]
//   uio[2]      in   s_strobe  rising edge = one ADC slot (or one config byte); async,
//                              2-flop synchroniser
//   uio[3]      in   s_frame   high with the strobe of slot 0 (sampled with the strobe)
//   uio[4]      out  overflow  sticky: the coder fell a frame behind and aborted a packet
//                              (cleared by enable = 0 or reset). Tells a chip-side drop from
//                              bytes lost in capture (the output has no back-pressure, D8)
//   uio[5]      in   cfg_en    1: strobes carry config bytes, address then data; async,
//                              2-flop synchroniser
//   uio[6]      out  m_valid   uo_out holds a byte in this clock
//   uio[7]      out  m_last    with m_valid: the byte on uo_out ends a packet
//                              m_last = 1 with m_valid = 0: abort token (D5/D7), the
//                              host drops its partial packet
//   uo_out[7:0] out  m_data; config readback while cfg_en = 1, CTRL.enable = 0 and an
//                    address byte was strobed without its data byte yet: uo_out = the
//                    register at that address (CTRL = {enable, 7'b0}, N_SEL, SEL_SLOT+i,
//                    other addresses 0), from the 3rd clock after the address strobe until
//                    the data strobe or cfg_en falls. Read: cfg_en = 1, strobe the address,
//                    wait >= 4 clocks, read uo_out, drop cfg_en (no write happens).
//   rst_n            async assert, synchronous release (2-flop reset synchroniser)
// All outputs come straight from flops (one output register stage).
//
// Output (decision D8, valid-only streaming): one byte per clock while m_valid, the abort
// token for one clock; the host must capture every clock (nothing can stall the output,
// like the merge circuitry / serializer of an implant). At the real frame rate the coder
// can never fall a frame behind for any data (scripts/proofs/output_bound.py), so the
// output never drops a packet; only a short frame (D5) or enable falling (D7) aborts one.
//
// Input protocol (C-IF-9): the host raises s_strobe (with s_frame for slot 0) and sets the
// sample in the same clock, holds s_strobe high for one clock (at least) and then low for at
// least one clock, and holds the sample for 2 clocks (one slot every 2 clocks at most).
module tt_um_nlc_compressor (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

  // CTS pads registers that share a clock net with clock gates (F19), so every register
  // group hangs off its own gate (the first one always on).
  wire clk_pins;
  nlc_icg u_cg_pins (.clk(clk), .en(1'b1), .gclk(clk_pins));

  // Reset synchroniser: rst_n asserts everything asynchronously (rst_s falls at once) and is
  // released on a clock edge, 2 flops later, so every async-reset flop downstream sees a
  // synchronous deassertion that STA checks (recovery / removal from rst_q[1]). rst_s is the
  // only reset inside the chip; the raw pin reaches these two flops only.
  reg [1:0] rst_q;
  always @(posedge clk_pins or negedge rst_n) begin
    if (!rst_n) rst_q <= 2'b00;
    else        rst_q <= {rst_q[0], 1'b1};
  end
  wire rst_s = rst_q[1];

  // Input pins. Clock-domain crossing, edge R0 = the first clock edge that samples s_strobe
  // high (stb_q[0] = 1):
  //   control  s_strobe and cfg_en: 2-flop synchronisers (stb_q[1:0], cfg_q[1:0]); stb_q[2]
  //            for edge detection. The strobe edge (stb_q[1] & ~stb_q[2]) is seen in the
  //            cycle after R1 and the core takes the slot at R2.
  //   s_frame  a 1-clock pulse with the strobe, so it is sampled like data, in the stage
  //            aligned with stb_q[0] (frm_q at R0), and moved to frame_q at R1.
  //   sample   not synchronised bit by bit (a bus): loaded once, at R1, while the host
  //            still holds it (2 clocks per slot), into ui_q/hi_q, stable from R1 until the
  //            core takes it at R2 and the next load.
  // The R1 loads are clock-gated with enables built from stb_q[0] & ~stb_q[1] (+ cfg_q[0],
  // frm_q, s_want): flop outputs, never a raw pin, so no input can glitch a gated clock.
  // Why stage 1 may drive these enables: the gate's latch samples its enable when it closes at
  // R1, so stb_q[0] (and frm_q, cfg_q[0], sampled at the same edge R0) get the whole cycle
  // minus clk->Q, the enable logic and the latch setup (~198 ns at 5 MHz) to resolve, the same
  // resolution time stb_q[1] gets: for this path the gate latch is the second synchroniser
  // stage. (While clk is low the latch is open, but gclk = clk & latch, so only the value at
  // R1 reaches gclk.) MTBF = exp(t_r / tau) / (T0 * f_clk * f_data); with assumed (not
  // sky130-characterised) tau = 50 ps and T0 = 1 ns, conservative for a 130 nm flop:
  // exp(198 ns / 50 ps) = e^3960 over 1e-9 * 5e6 * 2.5e6, i.e. unbounded in practice; even
  // at TT's 50 MHz maximum (t_r ~ 18 ns) it is e^360 / 1.25e5 s.
  // Strict alternative: capture the sample at R2 from stb_q[1]; the host would then hold the
  // sample >= 3 clocks (4 with margin), one slot per 4 clocks on the pin path, a C-IF-9
  // protocol change (the real frame, 128 slots, would need a 10 MHz clock). Or a 2-stage
  // delay-matched data pipe: +20 flops clocked every cycle (power, F19/F20).
  // Host requirement: s_frame and the sample are set up no later than s_strobe; the sample
  // stays stable until R1. With the host on the chip clock (TT demo board: the RP2040
  // drives clk) that is the 2 clocks of C-IF-9; a fully asynchronous host must add one clock
  // (the strobe can be first seen one edge late).
  reg  [2:0] stb_q;                   // s_strobe synchroniser + edge detect
  reg  [1:0] cfg_q;                   // cfg_en synchroniser
  reg        frm_q;                   // s_frame, sampled with stb_q[0]
  reg        frame_q;                 // s_frame of the slot, valid with the strobe edge
  reg  [7:0] ui_q;                    // sample[7:0] / config byte
  reg  [1:0] hi_q;                    // sample[9:8]
  wire       s_want;                  // from the core: the next slot strobe is selected

  always @(posedge clk_pins) begin
    if (!rst_s) begin
      stb_q <= 3'b000;
      cfg_q <= 2'b00;
      frm_q <= 1'b0;
    end else begin
      stb_q <= {stb_q[1:0], uio_in[2]};
      cfg_q <= {cfg_q[0], uio_in[5]};
      frm_q <= uio_in[3];
    end
  end

  // Power (F19/F20): the frame flag loads on every slot (the slot selector reads it on every
  // strobe); the sample / config byte only on a selected slot (s_want, registered state of the
  // slot selector, valid for the next strobe), a frame slot or a config strobe.
  wire stb_rise = stb_q[0] & ~stb_q[1];
  wire ld_frame = !rst_s || stb_rise;
  wire ld_data  = !rst_s || (stb_rise && (cfg_q[0] || frm_q || s_want));
  wire clk_frame, clk_data;
  nlc_icg u_cg_frm (.clk(clk), .en(ld_frame), .gclk(clk_frame));
  nlc_icg u_cg_dat (.clk(clk), .en(ld_data),  .gclk(clk_data));

  always @(posedge clk_frame) begin
    if (!rst_s) frame_q <= 1'b0;
    else        frame_q <= frm_q;
  end

  always @(posedge clk_data) begin
    if (!rst_s) begin
      ui_q <= 8'd0;
      hi_q <= 2'd0;
    end else begin
      ui_q <= ui_in;
      hi_q <= uio_in[1:0];
    end
  end

  wire strobe = stb_q[1] & ~stb_q[2];
  wire cfg_en = cfg_q[1];

  // config bytes: address, then data. cfg_addr/cfg_data are clock-gated (written on a
  // config strobe only; read only with cfg_we, so they need no reset).
  reg        have_addr;
  reg        cfg_we;
  wire [7:0] cfg_addr;
  wire [7:0] cfg_data;
  wire       cfg_strobe = rst_s & cfg_en & strobe;

  nlc_greg #(.W(8)) u_cfg_addr (.clk(clk), .en(cfg_strobe & ~have_addr), .d(ui_q), .q(cfg_addr));
  nlc_greg #(.W(8)) u_cfg_data (.clk(clk), .en(cfg_strobe &  have_addr), .d(ui_q), .q(cfg_data));

  always @(posedge clk_pins) begin
    if (!rst_s) begin
      have_addr <= 1'b0;
      cfg_we    <= 1'b0;
    end else begin
      cfg_we <= 1'b0;
      if (!cfg_en) begin
        have_addr <= 1'b0;
      end else if (strobe) begin
        have_addr <= ~have_addr;
        cfg_we    <= have_addr;
      end
    end
  end

  wire [7:0] m_data;
  wire       m_valid;
  wire       m_last;
  wire       m_abort;
  wire       overflow;
  wire       enabled;
  wire [7:0] cfg_rdata;

  nlc_core core (
      .clk(clk), .rst_n(rst_s),
      .s_valid(strobe & ~cfg_en), .s_frame(frame_q), .s_data({hi_q, ui_q}), .s_want(s_want),
      .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .m_data(m_data), .m_valid(m_valid), .m_last(m_last), .m_abort(m_abort),
      .enabled(enabled), .cfg_rdata(cfg_rdata), .overflow(overflow)
  );

  // Output register stage: the pads see flop outputs only (+1 clock of latency). The byte
  // loads only with m_valid or in config readback (its own gate; no reset, read only with
  // m_valid / in readback); the flags run on the always-on pin clock. Readback needs
  // enable = 0, so m_valid = 0 then (the two never share the register).
  wire       rb_mode = cfg_en & have_addr & ~enabled;
  reg        valid_q, last_q, ovf_q;
  wire [7:0] data_q;
  nlc_greg #(.W(8)) u_out (.clk(clk), .en(m_valid | rb_mode),
                           .d(rb_mode ? cfg_rdata : m_data), .q(data_q));

  always @(posedge clk_pins) begin
    if (!rst_s) begin
      valid_q <= 1'b0;
      last_q  <= 1'b0;
      ovf_q   <= 1'b0;
    end else begin
      valid_q <= m_valid;
      last_q  <= m_abort | m_last;              // core: m_last implies m_valid
      ovf_q   <= overflow;
    end
  end

  assign uo_out  = data_q;
  assign uio_out = {last_q, valid_q, 1'b0, ovf_q, 4'b0};
  assign uio_oe  = 8'b1101_0000;

  wire _unused = &{ena, uio_in[7:6], uio_in[4], 1'b0};

endmodule
