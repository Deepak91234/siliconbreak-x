// Key register feeding an on-chip crypto engine.
//
// `load` captures key_in and marks the key valid. `zeroize` (which wins over a
// simultaneous load) erases the key when software or a tamper response asks for
// it. key_out is wired to the engine's key input.
`default_nettype none
module key_vault (
    input  wire        clk,
    input  wire        reset,      // synchronous, active high
    input  wire        load,
    input  wire [15:0] key_in,
    input  wire        zeroize,
    output wire [15:0] key_out,
    output wire        key_valid
);
    reg [15:0] key_q;
    reg        valid_q;

    always @(posedge clk) begin
        if (reset) begin
            key_q   <= 16'h0000;
            valid_q <= 1'b0;
        end else if (zeroize) begin
            key_q      <= 16'h0000;   // erase every key bit, not just part of it
            valid_q    <= 1'b0;
        end else if (load) begin
            key_q   <= key_in;
            valid_q <= 1'b1;
        end
    end

    assign key_out   = key_q;
    assign key_valid = valid_q;
endmodule
`default_nettype wire
