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

  reg [8:0]  mem [0:DEPTH-1];
  reg [AW:0] wr_ptr, rd_ptr;

  assign count   = wr_ptr - rd_ptr;
  assign full    = (count == DEPTH);
  assign m_valid = (count != 0);
  assign m_data  = mem[rd_ptr[AW-1:0]][7:0];
  assign m_last  = mem[rd_ptr[AW-1:0]][8];

  always @(posedge clk) begin
    if (!rst_n) begin
      wr_ptr   <= 0;
      rd_ptr   <= 0;
      overflow <= 1'b0;
    end else begin
      if (wr_en) begin
        if (full) begin
          overflow <= 1'b1;
        end else begin
          mem[wr_ptr[AW-1:0]] <= {wr_last, wr_data};
          wr_ptr <= wr_ptr + 1'b1;
        end
      end
      if (m_valid && m_ready) rd_ptr <= rd_ptr + 1'b1;
    end
  end

endmodule
