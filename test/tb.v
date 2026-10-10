`default_nettype none
`timescale 1ns / 1ps

// Tiny Tapeout template testbench plus named wires for the cocotb harness.
module tb ();

  // +vcd=<file> [+vcd_start=<ns>]: instead of tb.fst, dump only the design's top-level nets
  // (every net of a flat gate-level netlist) for activity-based power (flow.py step layout)
  reg [8*512-1:0] vcd_file;
  reg [63:0]      vcd_start;
  initial begin
    if ($value$plusargs("vcd=%s", vcd_file)) begin
      if (!$value$plusargs("vcd_start=%d", vcd_start)) vcd_start = 0;
      #(vcd_start);
      $dumpfile(vcd_file);
      $dumpvars(1, user_project);
    end else if (!$test$plusargs("nodump")) begin
      $dumpfile("tb.fst");
      $dumpvars(0, tb);
      #1;
    end
  end

`ifdef SDF
  // T-GL-3: back-annotate the routed netlist with LibreLane's SDF (make GATES=sdf)
  initial $sdf_annotate(`SDF_FILE, user_project);
  // evidence that the delays are annotated: clock pin -> registered output pins, new extremes
  real t_clk, d, d_min, d_max;
  initial begin t_clk = 0; d_min = 1e9; d_max = 0; end
  always @(posedge clk) t_clk = $realtime;
  always @(uo_out or uio_out) if (rst_n === 1'b1 && t_clk > 0) begin
    d = $realtime - t_clk;
    if (d < d_min) begin d_min = d; $display("SDF clk->out delay: new min %0.3f ns", d); end
    if (d > d_max) begin d_max = d; $display("SDF clk->out delay: new max %0.3f ns", d); end
  end
`endif

  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;
`ifdef GL_TEST
  wire VPWR = 1'b1;
  wire VGND = 1'b0;
`endif

  tt_um_nlc_compressor user_project (
`ifdef GL_TEST
      .VPWR(VPWR),
      .VGND(VGND),
`endif
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

  // named views of the output pins (see src/project.v pin map)
  wire [7:0] m_data  = uo_out;
  wire       m_valid = uio_out[6];
  wire       m_last  = uio_out[7];
  wire       overflow = uio_out[4];

endmodule
