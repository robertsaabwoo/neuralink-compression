`default_nettype none

// Output byte FIFO with an end-of-packet bit. The encoder writes; the merge
// circuitry (or, on Tiny Tapeout, the host) reads with valid/ready.
// Writing while full drops the byte and sets the sticky overflow flag.
module nlc_out_fifo #(
    parameter DEPTH = 8,
    parameter AW    = $clog2(DEPTH)
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         wr_en,
    input  wire [7:0]   wr_data,
    input  wire         wr_last,
    output wire         full,
    output wire [AW:0]  count,
    output wire         m_valid,
    output wire [7:0]   m_data,
    output wire         m_last,
    input  wire         m_ready,
    output reg          overflow
);

  // Clock-gated: each entry only sees a clock edge when written, the pointers on a
  // push, a pop or reset.
  wire [9*DEPTH-1:0] mem;                 // entry k = mem[9k +: 9] = {last, data}
  reg  [AW:0]        wr_ptr, rd_ptr;
  wire [8:0]         head = mem[9*rd_ptr[AW-1:0] +: 9];

  assign count   = wr_ptr - rd_ptr;
  assign full    = (count == DEPTH);
  assign m_valid = (count != 0);
  assign m_data  = head[7:0];
  assign m_last  = head[8];

  // parent gate (push or pop); the entries' gates hang under it. Pointers clear
  // asynchronously: a synchronous reset through logic stayed X in gate-level sim.
  wire gclk;
  nlc_icg u_cg (.clk(clk), .en(wr_en || (m_valid && m_ready)), .gclk(gclk));

  genvar k;
  generate
    for (k = 0; k < DEPTH; k = k + 1) begin : g_mem
      nlc_greg #(.W(9)) u_e (.clk(gclk), .en(rst_n && wr_en && !full && wr_ptr[AW-1:0] == k),
                             .d({wr_last, wr_data}), .q(mem[9*k +: 9]));
    end
  endgenerate

  always @(posedge gclk or negedge rst_n) begin
    if (!rst_n) begin
      wr_ptr   <= 0;
      rd_ptr   <= 0;
      overflow <= 1'b0;
    end else begin
      if (wr_en) begin
        if (full) overflow <= 1'b1;
        else      wr_ptr   <= wr_ptr + 1'b1;
      end
      if (m_valid && m_ready) rd_ptr <= rd_ptr + 1'b1;
    end
  end

endmodule
