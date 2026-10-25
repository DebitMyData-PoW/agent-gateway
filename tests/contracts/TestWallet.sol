// SPDX-License-Identifier: CC0-1.0
pragma solidity ^0.8.0;

// Minimal smart-contract wallet for tests: ERC-1271 signatures are its owner's signatures.
contract TestWallet {
    address public immutable owner;

    constructor(address _owner) {
        owner = _owner;
    }

    function isValidSignature(bytes32 hash, bytes calldata signature) external view returns (bytes4) {
        if (signature.length != 65) return 0xffffffff;
        bytes32 r = bytes32(signature[0:32]);
        bytes32 s = bytes32(signature[32:64]);
        uint8 v = uint8(signature[64]);
        return ecrecover(hash, v, r, s) == owner ? bytes4(0x1626ba7e) : bytes4(0xffffffff);
    }
}

// Deploys TestWallet with CREATE2, so its address is known before it exists.
contract TestWalletFactory {
    function deploy(address owner, bytes32 salt) external returns (address wallet) {
        wallet = address(new TestWallet{salt: salt}(owner));
    }

    function addressOf(address owner, bytes32 salt) external view returns (address) {
        bytes32 hash = keccak256(abi.encodePacked(bytes1(0xff), address(this), salt,
            keccak256(abi.encodePacked(type(TestWallet).creationCode, abi.encode(owner)))));
        return address(uint160(uint256(hash)));
    }
}
